# Capstart — End-to-End Incident Flow

This document records what actually happens (and what was observed during a live end-to-end run)
as an incident travels through the Capstart remediation agent pipeline.

The guiding invariant for the whole system: **nothing touches the cluster until a human clicks
Approve.** Every LLM in the pipeline can only *narrow or defer* the path to action (gather more
evidence, lower confidence, escalate) — it can never *widen* it. The actual remediation action,
its target namespace/resource, and the execution are all deterministic and human-gated.

---

## Two Front Doors

An incident can be opened from either of two independent triggers, and both feed the **same**
diagnosis/planning graph via `webhook.run_incident_pipeline`:

| Trigger | Source | Entry point | Enabled by |
|---|---|---|---|
| **Metric alert** | Prometheus rule → Alertmanager | `POST /webhook/alertmanager` (`webhook.py`) | always on |
| **Error log line** | Loki query poll | background task (`log_watcher.py`) | `log_trigger_enabled` (default off) |

The log watcher polls Loki every `log_poll_interval_seconds` for lines matching
`log_error_pattern`, collapses each line to a stable signature (IPs/numbers/hex masked) so noisy
repeats dedup to one incident, classifies it (`diagnosis/log_classifier.py`), and opens an
incident with a 15-minute per-signature cooldown. From `run_incident_pipeline` onward the two
paths are identical.

---

## The Pipeline at a Glance

```
Fault happens
    │
    ├─────────────────────────────┐
    ▼                             ▼
Prometheus scrapes metrics     Loki ingests container logs
(kube-state-metrics /          (promtail → loki)
 blackbox / node-exporter)         │  log_watcher.py polls Loki for
    │  alert rule fires             │  /error|panic|oomkilled|…/ lines
    │  (e.g. PodCrashLoopBackOff,   │  (only if log_trigger_enabled)
    │   DeploymentReplicaMismatch)  │  dedup by signature + 15m cooldown
    │  for: 15s                     │
    ▼                             │
Alertmanager                       │
    │  POST /webhook/alertmanager   │  classify_log_event()
    ▼                             ▼
Agent – webhook.py / log_watcher.py ───────────────────────────────────────────┐
    │  Resolved alert?  → mark matching incident resolved, stop                 │
    │  Duplicate fingerprint still pending?  → ignore, stop                     │
    │  1. Log full payload                                                     │
    │  2. classify_alert() → incident_type / namespace / resource_name        │
    │  3. audit.create_incident() → incident_id (status=pending)              │
    │  4. Build initial IncidentState                                          │
    │  5. graph.ainvoke(initial_state)  ──────────────────────────────────┐   │
    │                                                                     │   │
    │  LangGraph pipeline (graph.py)                                      │   │
    │  ┌──────────────────────────────────────────────────────────────┐   │   │
    │  │  START                                                        │   │   │
    │  │    │                                                          │   │   │
    │  │    ▼                                                          │   │   │
    │  │  investigate  ◄──────────────────────────────────────────┐   │   │   │
    │  │    │  Always: deterministic PromQL/LogQL fan-out →        │   │   │   │
    │  │    │          state.context (precise numbers)             │   │   │   │
    │  │    │  If agentic_investigation: a ReAct agent also        │   │   │   │
    │  │    │  picks its own read-only diagnostics and writes      │   │   │   │
    │  │    │  a root-cause summary + evidence_trail (narrative    │   │   │   │
    │  │    │  on top; it never replaces the numbers)              │   │   │   │
    │  │    ▼                                                      │   │   │   │
    │  │  assess_severity   (deterministic — heuristics.py)        │   │   │   │
    │  │    │  Score P1–P4 from restart counts / replica gaps      │   │   │   │
    │  │    ▼                                                      │   │   │   │
    │  │  historical                                               │   │   │   │
    │  │    │  Retrieve ≤3 similar past incidents from DB          │   │   │   │
    │  │    ▼                                                      │   │   │   │
    │  │  supervisor                                               │   │   │   │
    │  │    │  Deterministic (default): P4 + strong same-resource  │   │   │   │
    │  │    │    historical match → auto_plan, else rca            │   │   │   │
    │  │    │  Agentic (agentic_supervisor): LLM picks             │   │   │   │
    │  │    │    gather_more | auto_plan | full_rca | escalate     │   │   │   │
    │  │    │    — BACKSTOPPED: auto_plan honoured only if a real  │   │   │   │
    │  │    │    candidate exists; gather_more capped by           │   │   │   │
    │  │    │    agent_max_steps. LLM can narrow/defer, not widen. │   │   │   │
    │  │    ├──► gather_more ──────────────────────────────────────┘   │   │   │
    │  │    ├──► escalate  (hand straight to human, skip planning)     │   │   │
    │  │    ├──► auto_plan (P4 + similarity ≥ 0.7, same resource AND   │   │   │
    │  │    │              severity, candidate actually recovered —    │   │   │
    │  │    │              replays its plan, re-locks target to THIS   │   │   │
    │  │    │              incident, no RCA/Plan LLM call)             │   │   │
    │  │    └──► rca       (everything else)                           │   │   │
    │  │              │                                                │   │   │
    │  │              │  Bedrock generates:                           │   │   │
    │  │              │    • diagnosis_text (free-form narrative)     │   │   │
    │  │              │    • confidence_score  0.0–1.0                │   │   │
    │  │              │                                               │   │   │
    │  │              ├── confidence < 0.4 ──► escalate              │   │   │
    │  │              └── confidence ≥ 0.4 ──► plan                  │   │   │
    │  │                                          │                   │   │   │
    │  │  plan (Bedrock tool-call)                │                   │   │   │
    │  │    │  LLM calls one of the allow-listed  │                   │   │   │
    │  │    │  remediation tools as a Bedrock      │                   │   │   │
    │  │    │  toolUse, supplying:                 │                   │   │   │
    │  │    │    • title  (human-readable)         │                   │   │   │
    │  │    │    • steps  (2-4 ordered steps)      │                   │   │   │
    │  │    │  Tool name = the action to execute   │                   │   │   │
    │  │    │  (NOT a free-form command)            │                   │   │   │
    │  │    ▼                                      │                   │   │   │
    │  │  reflect  (self-critique, if              │                   │   │   │
    │  │    │  agentic_reflection)                 │                   │   │   │
    │  │    │  LLM reviews plan vs evidence; shares │                   │   │   │
    │  │    │  the agent_max_steps hop budget with  │                   │   │   │
    │  │    │  supervisor's gather_more loop        │                   │   │   │
    │  │    ├──► investigate (needs more data) ────┘                   │   │   │
    │  │    └──► guardrail                                             │   │   │
    │  │              │                                                │   │   │
    │  │  guardrail  (deterministic, no LLM)                          │   │   │
    │  │    │  Checks:                                                 │   │   │
    │  │    │    ✓ action is in the incident_type's allow-list         │   │   │
    │  │    │    ✓ target namespace/name matches the alert's own       │   │   │
    │  │    ├──► FAIL → escalate                                       │   │   │
    │  │    └──► PASS → END  (status = pending_approval)              │   │   │
    │  │                                                               │   │   │
    │  │  escalate → END  (status = escalated)                        │   │   │
    │  └──────────────────────────────────────────────────────────────┘   │   │
    │                                                                      │   │
    │  6. audit.update_incident(diagnosis, plan, router_decision …)  ◄────┘   │
    └─────────────────────────────────────────────────────────────────────────┘
         │
         ▼
Human Approval UI  (http://localhost:8000)
    │  GET  /                      → incident queue
    │  GET  /incident/{id}         → full detail: diagnosis, context, plan, timeline
    │
    ├──► POST /incident/{id}/reject   → status=rejected, nothing touches the cluster
    │
    └──► POST /incident/{id}/approve
              │
              ▼
         executor.py  (runs in FastAPI BackgroundTask)
              │  Dispatches to the registered handler for plan.action
              │  Loops up to 60 attempts × 5 s:
              │    attempt_fn(incident_id, namespace, resource_name)
              │    check_fn(namespace, resource_name)  ← same signal Prometheus alerts on
              │    on_update(status, result)           ← live DB update visible in UI
              │
              ├── resolved → status=executed, audit logged
              └── budget exhausted → status=execution_failed
```

---

## Node-by-Node

| Node | LLM? | What it produces | Failure / fallback |
|---|---|---|---|
| `investigate` | optional | `context` (deterministic numbers) + optional ReAct `evidence_trail` / summary | ReAct errors fall back to deterministic context |
| `assess_severity` | no | `severity` P1–P4 + rationale from `heuristics.py` thresholds | pure rule, always runs |
| `historical` | no | ≤3 similar past incidents (same type) from the audit DB | empty list if none |
| `supervisor` | optional | `router_decision` ∈ {gather_more, auto_plan, full_rca, escalate} + rationale | LLM unavailable / bad output → forces `full_rca` |
| `auto_plan` | no | replayed plan from a trusted historical candidate, target re-locked | no candidate → escalate |
| `rca` | yes | `diagnosis_text` + `confidence_score` | confidence < 0.4 → escalate |
| `plan` | yes | plan `title`/`steps` + `action` chosen via Bedrock toolUse | LLM action still re-checked by guardrail |
| `reflect` | optional | verdict ok/insufficient; may loop to investigate | budget exhausted → proceed to guardrail |
| `guardrail` | no | PASS → `pending_approval`; FAIL → escalate | deterministic allow-list + target re-lock |
| `escalate` | no | `escalation_reason`, END | terminal (human picks up) |

**Shared hop budget.** Both `supervisor → gather_more → investigate` and
`reflect → investigate` loops increment `supervisor_hops` and are hard-capped by
`agent_max_steps`, so the agent can never spin forever before reaching a human gate.

---

## Agentic vs Deterministic Modes

Four nodes have an LLM-driven "agentic" mode and a deterministic fallback, each toggled in
`config.py` (overridable by env var). Agentic modes also require `bedrock_enabled`. The key
safety property holds in every mode: **an LLM can only narrow or defer the path to action
(gather more, lower confidence, escalate), never widen it** — the action, its target, and
execution stay deterministic and human-gated.

| Setting | Default | Effect when on |
|---|---|---|
| `agentic_investigation` | on | `investigate` runs a ReAct read-only diagnostic loop on top of the fixed PromQL/LogQL fan-out |
| `agentic_supervisor` | on | `supervisor` routes via an LLM decision (still deterministically backstopped) |
| `agentic_reflection` | on | adds the self-critique `reflect` node before the guardrail |
| `llm_authors_action` | on | Bedrock picks the action via toolUse (guardrail still rejects anything not allow-listed for the type) |
| `agentic_log_classification` | off | LLM classifies ambiguous Loki log lines; below `log_classification_min_confidence` stays `unknown` → escalate |
| `log_trigger_enabled` | off | enables the Loki error-log second front door (`log_watcher.py`) |
| `agent_max_steps` | 6 | hard cap on ReAct tool calls and the gather_more / reflect loop hops |

With every agentic flag off, the graph degrades to a fully deterministic, unit-testable
pipeline: `investigate → assess_severity → historical → (auto_plan | rca → plan) → guardrail`.

---

## Observed E2E Run (Incident #17)

### 1. Fault Injection
```
python project/injector/crashloop.py
```
- Patched `demo/demo-web` deployment to run a crash-on-start command.
- Pod `demo-web-648db658f7-kd6pp` entered `CrashLoopBackOff`.

### 2. Prometheus Alert Fired
- Alert: **`DeploymentReplicaMismatch`** (expr: `spec_replicas != available_replicas`, for: 15s)
- Alertmanager POSTed the alert to `http://agent.agent.svc/webhook/alertmanager`.

### 3. Agent Classified the Alert
- `classify_alert()` mapped `DeploymentReplicaMismatch` → `incident_type=replica_mismatch`
- `audit.create_incident()` created **incident #17** with `status=pending`.

### 4. LangGraph Pipeline Ran
Audit trail for incident #17 (from `incident_steps` table):

| Step | Direction | What happened |
|---|---|---|
| `webhook.classify` | INFO | Rule engine classified alert as `replica_mismatch` |
| `prometheus.query` ×5 | SEND/RECV | Pulled deployment replicas, pod status, restart counts |
| `loki.query` | SEND/RECV | Pulled recent container logs |
| `investigate.agent` | INFO | Agent summarised root cause from evidence |
| `supervisor.route` | SEND/RECV | Bedrock decided: run full RCA |
| `history.retrieve` | INFO | Retrieved 3 similar past `replica_mismatch` incidents |
| `bedrock.converse` | SEND/RECV | Bedrock generated diagnosis text + confidence=0.8 |
| `plan.tool_call` | SEND | Offered 2 tools: `rollout_restart_deployment`, `delete_pod` |
| `plan.tool_call` | RECV | **LLM chose `rollout_restart_deployment` via tool call** |
| `reflect.review` | SEND/RECV | Self-critique passed; no extra evidence needed |
| `remediation.plan_ready` | INFO | Plan passed guardrail — awaiting human approval |

### 5. LLM-Authored Plan (NOT a hardcoded template)
The plan stored in the DB (`selected_via=tool_call`):
```json
{
  "title": "Restart deployment to resolve replica mismatch",
  "steps": [
    "Restart the 'demo-web' deployment to recreate the missing replica.",
    "Trigger a rolling restart to ensure the deployment reaches the desired state of 2 available replicas.",
    "Monitor the deployment status to confirm that the missing replica is successfully created and available."
  ],
  "action": "rollout_restart_deployment",
  "selected_via": "tool_call",
  "target": {"namespace": "demo", "name": "demo-web"},
  "generated_at": "2026-10-01T10:14:50.042430+00:00"
}
```
> **Key point:** The `title` and `steps` are generated fresh by Bedrock from the live evidence
> (not copied from `remediation/templates.py`). The `action` is validated by the guardrail
> against the allow-list for `replica_mismatch`.

### 6. Human Approval
```
POST http://localhost:8000/incident/17/approve
approver=atonis-e2e-test
```
- UI returned 303 → redirected to incident detail page.
- `audit_log` step: `ui.decision ACTION — Human approved the remediation plan`.

### 7. Executor Fixed the Cluster
- `rollout_restart_deployment` handler:
  - Restored last-known-good image from annotation `capstart.dev/original-image`.
  - Triggered `kubectl rollout restart deployment/demo-web -n demo`.
  - Polled until `availableReplicas == specReplicas`.
- **Resolved in attempt 1** (< 5 seconds).
- Final execution result:
  ```json
  {"deployment": "demo-web", "known_good_restored": true, "rollout_restart_triggered": true, "attempt": 1, "status": "executed"}
  ```
- Incident #17 status → **`executed`**.

### 8. Verification
```
kubectl get pods -n demo -l app=demo-web
NAME                        READY   STATUS    RESTARTS   AGE
demo-web-8674d44c78-8zb42   1/1     Running   0          46s
demo-web-8674d44c78-gxxxh   1/1     Running   0          54s
```
Both replicas healthy. Prometheus alert subsequently resolved.

---

## Key Design Points

| Concern | How it's handled |
|---|---|
| **LLM inventing dangerous commands** | LLM only calls a named tool from a fixed allow-list; it cannot invent a new action or a free-form `kubectl` command |
| **LLM targeting the wrong resource** | Guardrail (and `auto_plan`) re-lock `target.namespace` and `target.name` to the alert's own values server-side |
| **LLM widening the actuation path** | Every agentic decision can only narrow or defer; deterministic backstops force `full_rca` when the LLM is unavailable, returns garbage, or claims an auto-plan candidate that doesn't exist |
| **Low-confidence diagnosis** | `rca` node escalates instead of planning when `confidence < 0.4` |
| **Agent looping forever** | `gather_more` and `reflect` share one `agent_max_steps` hop budget, after which the graph is forced forward |
| **Plan that fails the guardrail** | Escalated to human instead of shown for approval |
| **Duplicate / resolved alerts** | Same fingerprint while pending is ignored; a resolved alert marks the matching incident `resolved` without re-running the graph |
| **Noisy repeated log errors** | Log watcher collapses lines to a stable signature and applies a 15-minute per-signature cooldown |
| **Human always in control** | No code path executes anything without an explicit `Approve` click |
| **Auditability** | Every Prometheus/Loki/Bedrock/K8s API call is logged per-incident in `incident_steps` |

---

## Allowlisted Remediation Actions

| Action | When used |
|---|---|
| `delete_pod` | Crash-looping pod due to broken config/command |
| `rollout_restart_deployment` | Bad image/rollout left replicas unavailable |
| `fix_service_selector` | Service has zero endpoints (selector mismatch) |
| `delete_blocking_networkpolicy` | NetworkPolicy denying traffic (`chaos=injected` label) |
| `recover_node` | Node gone `NotReady` — cordon, wait for kubelet, uncordon |

---

## Files That Implement Each Stage

| Stage | File(s) |
|---|---|
| Alert rules | `monitoring/prometheus/prometheus-config.yaml` |
| Alertmanager routing | `monitoring/prometheus/alertmanager-config.yaml` |
| Webhook receiver (metric front door) | `agent/app/webhook.py` |
| Log watcher (Loki front door) | `agent/app/log_watcher.py` |
| Alert classifier | `agent/app/diagnosis/classifier.py` |
| Log-line classifier | `agent/app/diagnosis/log_classifier.py` |
| Evidence / context builder | `agent/app/diagnosis/context.py` |
| Configuration & toggles | `agent/app/config.py` |
| Graph wiring | `agent/app/graph/graph.py` |
| Investigate node | `agent/app/graph/nodes/investigate.py` |
| ReAct investigator + read-only tools | `agent/app/graph/react_investigator.py` + `agent/app/graph/tools.py` |
| Severity heuristics | `agent/app/graph/heuristics.py` |
| Auto-plan node | `agent/app/graph/nodes/auto_plan.py` |
| Severity node | `agent/app/graph/nodes/severity.py` |
| Historical node | `agent/app/graph/nodes/historical.py` |
| Supervisor node | `agent/app/graph/nodes/supervisor.py` |
| RCA node | `agent/app/graph/nodes/rca.py` |
| Plan node | `agent/app/graph/nodes/plan.py` |
| Reflect node | `agent/app/graph/nodes/reflect.py` |
| Guardrail node | `agent/app/graph/nodes/guardrail.py` + `agent/app/graph/guardrail.py` |
| Escalate node | `agent/app/graph/nodes/escalate.py` |
| Action allow-list | `agent/app/remediation/templates.py` |
| Approval UI | `agent/app/ui/routes.py` + `agent/app/ui/templates/` |
| Executor | `agent/app/executor.py` |
| Audit DB | `agent/app/audit.py` |
| Bedrock client | `agent/app/bedrock_client.py` |
