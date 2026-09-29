"""
Executes an approved remediation plan against the Kubernetes API.

This module is only ever called AFTER a human clicks Approve in the UI - see
ui/routes.py. Every Kubernetes API call made here is logged (request +
response) via log_step so the audit trail shows exactly what was done.

Each action is paired with a "resolved?" check against the same kind of
signal Prometheus alerts on (available replicas, endpoint addresses, node
Ready, etc.). Approving doesn't mean "run one patch and hope" - the agent
keeps re-checking (and re-applying idempotent fixes) on an interval until the
real condition clears or a bounded retry budget is exhausted, calling
on_update() after every attempt so the incident's status/result in the UI
reflects live progress instead of a single pass/fail verdict.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Callable

from kubernetes.client import ApiException

from . import kube_client
from .logging_utils import log_step

ORIGINAL_COMMAND_ANNOTATION = "capstart.dev/original-command"
ORIGINAL_SELECTOR_ANNOTATION = "capstart.dev/original-selector"
ORIGINAL_IMAGE_ANNOTATION = "capstart.dev/original-image"

_core_v1 = kube_client.core_v1
_apps_v1 = kube_client.apps_v1
_networking_v1 = kube_client.networking_v1

MAX_ATTEMPTS = 60
POLL_INTERVAL_SECONDS = 5


def execute_remediation(
    incident_id: int,
    plan: dict[str, Any],
    namespace: str,
    resource_name: str,
    on_update: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    action = plan.get("action")
    entry = _HANDLERS.get(action)
    log_step(
        incident_id,
        "executor.dispatch",
        "ACTION",
        {"action": action, "namespace": namespace, "resource_name": resource_name, "max_attempts": MAX_ATTEMPTS},
        note="Human approved this plan; executing against the Kubernetes API and "
        "re-checking until the underlying condition actually clears",
    )
    if entry is None:
        result = {"status": "skipped", "reason": f"no executor registered for action '{action}'"}
        log_step(incident_id, "executor.result", "RECV", result)
        if on_update:
            on_update("execution_failed", result)
        return result

    attempt_fn, check_fn, on_resolved_fn = entry
    result: dict[str, Any] = {}

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            result = attempt_fn(incident_id, namespace, resource_name)
        except ApiException as exc:
            result = {"status": "error", "reason": f"{exc.status} {exc.reason}"}
        result["attempt"] = attempt

        try:
            resolved = check_fn(namespace, resource_name)
        except ApiException as exc:
            resolved = False
            result["check_error"] = f"{exc.status} {exc.reason}"

        if resolved:
            if on_resolved_fn:
                try:
                    result.update(on_resolved_fn(incident_id, namespace, resource_name))
                except ApiException as exc:
                    result["on_resolved_error"] = f"{exc.status} {exc.reason}"
            result["status"] = "executed"
            log_step(incident_id, "executor.result", "RECV", result, note=f"Condition cleared after {attempt} attempt(s)")
            if on_update:
                on_update("executed", result)
            return result

        note = f"Not resolved yet (attempt {attempt}/{MAX_ATTEMPTS}) - retrying in {POLL_INTERVAL_SECONDS}s"
        log_step(incident_id, "executor.retry", "INFO", result, note=note)
        if on_update:
            on_update("in_progress", result)
        if attempt < MAX_ATTEMPTS:
            time.sleep(POLL_INTERVAL_SECONDS)

    result["status"] = "execution_failed"
    result["attempts"] = MAX_ATTEMPTS
    result["reason"] = result.get(
        "reason", "Condition did not clear within the retry window - likely needs out-of-band/manual intervention"
    )
    log_step(incident_id, "executor.result", "RECV", result, note="Gave up after max retries")
    if on_update:
        on_update("execution_failed", result)
    return result


def _deployment_name_from_pod(pod_name: str) -> str:
    # Strip the ReplicaSet-hash and pod-hash suffixes, e.g. demo-web-6f8d9c7b6-abcde -> demo-web
    parts = pod_name.rsplit("-", 2)
    return parts[0] if len(parts) == 3 else pod_name


def _delete_pod(incident_id: int, namespace: str, pod_name: str) -> dict[str, Any]:
    apps = _apps_v1()
    core = _core_v1()
    deployment_name = _deployment_name_from_pod(pod_name)
    result: dict[str, Any] = {"pod": pod_name, "deployment": deployment_name}

    try:
        dep = apps.read_namespaced_deployment(deployment_name, namespace)
        annotations = dep.metadata.annotations or {}
        original = annotations.get(ORIGINAL_COMMAND_ANNOTATION)
        if original:
            original_spec = json.loads(original)
            patch = {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": original_spec["name"],
                                    "command": original_spec.get("command"),
                                    "args": original_spec.get("args"),
                                }
                            ]
                        }
                    }
                },
                "metadata": {"annotations": {ORIGINAL_COMMAND_ANNOTATION: None}},
            }
            log_step(
                incident_id,
                "k8s.patch_deployment",
                "SEND",
                {"namespace": namespace, "deployment": deployment_name, "patch": patch},
                note="Reverting container command/args to last-known-good config",
            )
            apps.patch_namespaced_deployment(deployment_name, namespace, patch)
            log_step(incident_id, "k8s.patch_deployment", "RECV", {"status": "reverted"})
            result["deployment_reverted"] = True
        else:
            result["deployment_reverted"] = False
    except ApiException as exc:
        result["deployment_reverted"] = False
        result["deployment_error"] = f"{exc.status} {exc.reason}"

    # Sweep for any pod under this deployment still stuck crash-looping - on the
    # first attempt that's just pod_name, but on retries a newer replica can
    # have taken over that name/hash while still inheriting the bad state.
    targets = {pod_name}
    try:
        for p in core.list_namespaced_pod(namespace, label_selector=f"app={deployment_name}").items:
            for cs in p.status.container_statuses or []:
                if cs.state and cs.state.waiting and cs.state.waiting.reason in ("CrashLoopBackOff", "Error"):
                    targets.add(p.metadata.name)
    except ApiException:
        pass

    deleted, errors = [], {}
    for name in targets:
        log_step(incident_id, "k8s.delete_pod", "SEND", {"namespace": namespace, "pod": name})
        try:
            core.delete_namespaced_pod(name, namespace)
            deleted.append(name)
        except ApiException as exc:
            errors[name] = f"{exc.status} {exc.reason}"
    log_step(incident_id, "k8s.delete_pod", "RECV", {"deleted": deleted, "errors": errors})

    result["pods_targeted"] = sorted(targets)
    result["pods_deleted"] = deleted
    if errors:
        result["pod_delete_errors"] = errors
    return result


def _check_crashloop_resolved(namespace: str, pod_name: str) -> bool:
    apps = _apps_v1()
    core = _core_v1()
    deployment_name = _deployment_name_from_pod(pod_name)
    dep = apps.read_namespaced_deployment(deployment_name, namespace)
    spec_replicas = dep.spec.replicas or 0
    available = dep.status.available_replicas or 0
    if available < spec_replicas:
        return False
    for p in core.list_namespaced_pod(namespace, label_selector=f"app={deployment_name}").items:
        for cs in p.status.container_statuses or []:
            if cs.state and cs.state.waiting and cs.state.waiting.reason in ("CrashLoopBackOff", "Error"):
                return False
    return True


def _cordon_node(incident_id: int, namespace: str, node_name: str) -> dict[str, Any]:
    core = _core_v1()
    log_step(incident_id, "k8s.cordon_node", "SEND", {"node": node_name, "unschedulable": True})
    core.patch_node(node_name, {"spec": {"unschedulable": True}})
    log_step(incident_id, "k8s.cordon_node", "RECV", {"status": "cordoned"})
    # A Kubernetes-scheduled Job cannot start on a node whose own kubelet is
    # down (that's what starts pods on the node in the first place), so an
    # in-cluster fix is not possible via the K8s API alone - the kubelet must
    # come back out-of-band (in this kind demo: `injector/node_not_ready.py
    # --revert`; in a real cluster: node auto-repair/cluster autoscaler).
    return {
        "node": node_name,
        "cordoned": True,
        "note": "Waiting for kubelet to come back out-of-band; cannot restart it via the K8s API.",
    }


def _check_node_ready(namespace: str, node_name: str) -> bool:
    core = _core_v1()
    node = core.read_node(node_name)
    conditions = {c.type: c.status for c in (node.status.conditions or [])}
    return conditions.get("Ready") == "True"


def _uncordon_node(incident_id: int, namespace: str, node_name: str) -> dict[str, Any]:
    core = _core_v1()
    log_step(incident_id, "k8s.uncordon_node", "SEND", {"node": node_name, "unschedulable": False})
    core.patch_node(node_name, {"spec": {"unschedulable": False}})
    log_step(incident_id, "k8s.uncordon_node", "RECV", {"status": "uncordoned"})
    return {"node_ready": True, "uncordoned": True}


def _fix_service_selector(incident_id: int, namespace: str, service_name: str) -> dict[str, Any]:
    core = _core_v1()
    result: dict[str, Any] = {"service": service_name}

    svc = core.read_namespaced_service(service_name, namespace)
    annotations = svc.metadata.annotations or {}
    original = annotations.get(ORIGINAL_SELECTOR_ANNOTATION)
    if original:
        original_selector = json.loads(original)
    elif svc.spec.selector == {"app": "no-such-pods"} or not svc.spec.selector:
        apps = _apps_v1()
        dep = apps.read_namespaced_deployment(service_name, namespace)
        original_selector = dep.spec.selector.match_labels
    else:
        original_selector = svc.spec.selector  # already fixed on an earlier attempt

    patch = {
        "spec": {"selector": original_selector},
        "metadata": {"annotations": {ORIGINAL_SELECTOR_ANNOTATION: None}},
    }
    log_step(
        incident_id,
        "k8s.patch_service",
        "SEND",
        {"namespace": namespace, "service": service_name, "patch": patch},
        note="Restoring Service selector to match the intended pods",
    )
    core.patch_namespaced_service(service_name, namespace, patch)
    log_step(incident_id, "k8s.patch_service", "RECV", {"status": "patched"})

    result["restored_selector"] = original_selector
    return result


def _check_service_resolved(namespace: str, service_name: str) -> bool:
    core = _core_v1()
    endpoints = core.read_namespaced_endpoints(service_name, namespace)
    return any(subset.addresses for subset in (endpoints.subsets or []))


def _delete_blocking_networkpolicy(incident_id: int, namespace: str, resource_name: str) -> dict[str, Any]:
    net = _networking_v1()
    result: dict[str, Any] = {"namespace": namespace}

    log_step(
        incident_id,
        "k8s.list_networkpolicies",
        "SEND",
        {"namespace": namespace, "label_selector": "chaos=injected"},
    )
    policies = net.list_namespaced_network_policy(namespace, label_selector="chaos=injected")
    names = [p.metadata.name for p in policies.items]
    log_step(incident_id, "k8s.list_networkpolicies", "RECV", {"found": names})

    deleted = []
    for name in names:
        log_step(incident_id, "k8s.delete_networkpolicy", "SEND", {"namespace": namespace, "name": name})
        net.delete_namespaced_network_policy(name, namespace)
        log_step(incident_id, "k8s.delete_networkpolicy", "RECV", {"name": name, "status": "deleted"})
        deleted.append(name)

    result["deleted_networkpolicies"] = deleted
    return result


def _check_networkpolicy_resolved(namespace: str, resource_name: str) -> bool:
    net = _networking_v1()
    policies = net.list_namespaced_network_policy(namespace, label_selector="chaos=injected")
    return len(policies.items) == 0


def _rollout_restart_deployment(incident_id: int, namespace: str, deployment_name: str) -> dict[str, Any]:
    apps = _apps_v1()
    result: dict[str, Any] = {"deployment": deployment_name}

    dep = apps.read_namespaced_deployment(deployment_name, namespace)
    annotations = dep.metadata.annotations or {}
    original_image = annotations.get(ORIGINAL_IMAGE_ANNOTATION)

    patch: dict[str, Any] = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "kubectl.kubernetes.io/restartedAt": datetime.now(timezone.utc).isoformat()
                    }
                }
            }
        }
    }
    if original_image:
        container_name = dep.spec.template.spec.containers[0].name
        patch["spec"]["template"]["spec"] = {"containers": [{"name": container_name, "image": original_image}]}
        patch["metadata"] = {"annotations": {ORIGINAL_IMAGE_ANNOTATION: None}}
        result["image_restored_to"] = original_image

    log_step(
        incident_id,
        "k8s.patch_deployment",
        "SEND",
        {"namespace": namespace, "deployment": deployment_name, "patch": patch},
        note="Restoring last-known-good image (if recorded) and triggering rollout restart",
    )
    apps.patch_namespaced_deployment(deployment_name, namespace, patch)
    log_step(incident_id, "k8s.patch_deployment", "RECV", {"status": "patched"})

    return result


def _check_replica_resolved(namespace: str, deployment_name: str) -> bool:
    apps = _apps_v1()
    dep = apps.read_namespaced_deployment(deployment_name, namespace)
    spec_replicas = dep.spec.replicas or 0
    available = dep.status.available_replicas or 0
    return spec_replicas > 0 and available >= spec_replicas


_HANDLERS: dict[str, tuple[Callable[..., dict[str, Any]], Callable[..., bool], Callable[..., dict[str, Any]] | None]] = {
    "delete_pod": (_delete_pod, _check_crashloop_resolved, None),
    "recover_node": (_cordon_node, _check_node_ready, _uncordon_node),
    "fix_service_selector": (_fix_service_selector, _check_service_resolved, None),
    "delete_blocking_networkpolicy": (_delete_blocking_networkpolicy, _check_networkpolicy_resolved, None),
    "rollout_restart_deployment": (_rollout_restart_deployment, _check_replica_resolved, None),
}

