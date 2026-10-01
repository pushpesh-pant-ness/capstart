"""
Remediation tools - the fixed, deterministic catalog of actions the agent can
ever execute. Bedrock only writes the human-readable diagnosis/narrative and
*chooses* which of these tools to apply; it can never invent a new action or a
free-form command. Each tool name maps 1:1 to a handler in executor._HANDLERS.

Per incident type we expose an ALLOW-LIST of candidate tools (``candidate_actions``)
the LLM may pick from, plus a ``default_action`` used when the LLM doesn't author
the choice. The guardrail (app/graph/guardrail.py) rejects any action outside the
incident type's allow-list, and the target namespace/resource is always re-locked
server-side to the alert's own - never taken from the LLM. Once approved, the
executor keeps re-checking the real underlying condition (available replicas,
endpoint addresses, node Ready, etc.) and retries until it clears or a bounded
retry budget runs out (see executor.py).
"""
from __future__ import annotations

# The remediation toolbelt: action name -> one-line description shown to the LLM
# so it can pick the appropriate tool. Every key must have a handler registered
# in executor._HANDLERS (and vice versa).
REMEDIATION_TOOLS: dict[str, str] = {
    "delete_pod": (
        "Revert a bad container command/config (if one was recorded) and delete the "
        "crash-looping pod(s) so the ReplicaSet reschedules fresh replicas. Best when a "
        "pod is crashing on start due to a broken command/args/config."
    ),
    "rollout_restart_deployment": (
        "Restore the Deployment's last-known-good image (if recorded) and trigger a "
        "rolling restart. Best when a bad image/rollout left replicas unavailable and a "
        "fresh rollout is needed."
    ),
    "fix_service_selector": (
        "Restore a Service's selector to match its intended pods so its Endpoints "
        "repopulate. Best when a Service has zero endpoints because its selector no "
        "longer matches any pods."
    ),
    "delete_blocking_networkpolicy": (
        "Delete the injected NetworkPolicy objects (labelled chaos=injected) that are "
        "denying traffic in the namespace. Best when connectivity is blocked by a "
        "NetworkPolicy rather than by an unhealthy workload."
    ),
    "recover_node": (
        "Cordon a NotReady node so nothing new schedules on it, wait for its kubelet to "
        "recover, then uncordon it. Best when a whole node has gone NotReady."
    ),
}

# Per incident type: the narrative title/steps, the allow-listed tools the LLM
# may choose among (candidate_actions), and the default used when the LLM does
# not author the action. candidate_actions must be a subset of REMEDIATION_TOOLS.
TEMPLATES: dict[str, dict] = {
    "crashloop": {
        "title": "Restart crash-looping pod (rollback bad config if present)",
        "steps": [
            "Inspect recent container logs (Loki panel) for the crash reason.",
            "If the Deployment template was changed to a broken command/config, revert it to the last-known-good version.",
            "Delete the crash-looping pod so the ReplicaSet reschedules a fresh replica.",
            "Keep checking until the Deployment has all replicas Available and no pod is still crash-looping.",
        ],
        "default_action": "delete_pod",
        "candidate_actions": ["delete_pod", "rollout_restart_deployment"],
    },
    "node_not_ready": {
        "title": "Cordon node and wait for kubelet to recover",
        "steps": [
            "Cordon the node so no new pods are scheduled onto it while it is unhealthy.",
            "Keep polling the node's Ready condition on an interval (up to the retry budget).",
            "A node whose own kubelet is down cannot run a Kubernetes-scheduled fix "
            "(nothing can start on it until kubelet is back) - kubelet must be restarted "
            "out-of-band (in this kind demo: injector/node_not_ready.py --revert).",
            "As soon as it reports Ready, uncordon the node so scheduling resumes.",
        ],
        "default_action": "recover_node",
        "candidate_actions": ["recover_node"],
    },
    "service_unreachable": {
        "title": "Restore Service endpoints",
        "steps": [
            "Verify the Service selector matches the labels on the intended pods.",
            "Restore the Service's selector to the last-known-good value.",
            "Keep checking until the Endpoints object lists at least one healthy address.",
        ],
        "default_action": "fix_service_selector",
        "candidate_actions": ["fix_service_selector"],
    },
    "networkpolicy_block": {
        "title": "Remove blocking NetworkPolicy",
        "steps": [
            "Identify NetworkPolicy objects in the namespace that could deny this traffic.",
            "Delete the offending NetworkPolicy (injected policies are labelled chaos=injected).",
            "Keep checking until no chaos=injected NetworkPolicy remains in the namespace.",
        ],
        "default_action": "delete_blocking_networkpolicy",
        "candidate_actions": ["delete_blocking_networkpolicy"],
    },
    "replica_mismatch": {
        "title": "Rollback bad image/config and restart Deployment rollout",
        "steps": [
            "Check the Deployment's pod template for a bad image/config causing failures.",
            "Restore the last-known-good image if one was recorded.",
            "Trigger a rollout restart to force fresh ReplicaSet pods.",
            "Keep checking until status.availableReplicas matches spec.replicas.",
        ],
        "default_action": "rollout_restart_deployment",
        "candidate_actions": ["rollout_restart_deployment", "delete_pod"],
    },
}


def allowed_actions(incident_type: str) -> set[str]:
    """The allow-listed remediation tools the LLM may choose for this incident
    type. Empty for an unrecognised type (the guardrail then escalates)."""
    template = TEMPLATES.get(incident_type)
    return set(template["candidate_actions"]) if template else set()


def default_action(incident_type: str) -> str | None:
    """The tool used when the LLM doesn't author the action (or picks an invalid one)."""
    template = TEMPLATES.get(incident_type)
    return template["default_action"] if template else None


def candidate_tools(incident_type: str) -> list[tuple[str, str]]:
    """(action, description) pairs for this incident type's allow-listed tools,
    for presenting the choice to the LLM in the plan prompt."""
    template = TEMPLATES.get(incident_type)
    if not template:
        return []
    return [(action, REMEDIATION_TOOLS[action]) for action in template["candidate_actions"]]
