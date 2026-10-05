Read memory 

Read 

Read 

Read 

Read 

Read 

Read 

Read 

Read 

Read 

Read , lines 1 to 120

# Capstart Agent — Full End-to-End Sequence

This walks through **every** step from the moment a fault starts producing telemetry to the moment the cluster is actually fixed and the alert clears. Two independent "front doors" exist (metrics→Prometheus and logs→Loki), but they converge into the exact same pipeline, so I'll cover both.

---

## Phase 1 — Telemetry Generation (inside the cluster)

A fault is injected (e.g. injector/crashloop.py) or happens naturally. From that moment, two parallel telemetry streams start flowing:

**Metrics side:**
- `kube-state-metrics` exposes Kubernetes object state as Prometheus metrics (`kube_pod_container_status_restarts_total`, `kube_node_status_condition`, `kube_endpoint_address_available`, `kube_deployment_status_replicas_unavailable`, etc.) — monitoring/kube-state-metrics
- `node-exporter` (DaemonSet) exposes host/node-level metrics — monitoring/node-exporter
- `blackbox-exporter` actively probes `http://demo-web.demo.svc.cluster.local` on a schedule (`module: http_2xx`) and exposes `probe_success` — used specifically so a `NetworkPolicy` blocking traffic shows up as a metric even though nothing "crashed"

**Log side:**
- Every container's stdout/stderr is tailed by `promtail` (DaemonSet) and shipped into `loki` as labeled log streams — monitoring/loki

---

## Phase 2 — Prometheus Scrapes

Per `prometheus-config.yaml`:
- `global.scrape_interval: 15s` — Prometheus pulls `/metrics` from `kube-state-metrics`, `node-exporter`, and itself every 15 seconds and stores the samples as a time series.
- The blackbox job is scraped the same way but via the `/probe` indirection (Prometheus hits blackbox-exporter, which in turn hits the real target and reports success/failure as the `probe_success` metric).

At this point nothing has "fired" yet — it's just raw time-series data accumulating.

---

## Phase 3 — PromQL Rule Evaluation (the actual alerting logic)

Per `global.evaluation_interval: 15s`, Prometheus re-runs every rule in rules.yml against the latest data, every 15 seconds. There are 5 rules, one per incident type:

| Alert | PromQL expression | `for:` |
|---|---|---|
| `PodCrashLoopBackOff` | `increase(kube_pod_container_status_restarts_total{namespace="demo"}[5m]) > 3` | 15s |
| `NodeNotReady` | `kube_node_status_condition{condition="Ready", status="true"} == 0` | 15s |
| `ServiceEndpointsUnreachable` | `kube_endpoint_address_available{namespace="demo"} == 0` | 15s |
| `NetworkPolicyBlockingTraffic` | `probe_success{job="blackbox-networkpolicy-probe"} == 0` | 15s |
| `DeploymentReplicaMismatch` | `kube_deployment_status_replicas_unavailable{namespace="demo"} > 0 unless on(namespace) count by (namespace)(kube_pod_container_status_waiting_reason{namespace="demo", reason="CrashLoopBackOff"}==1)` | 15s |

How this plays out:
1. The PromQL expression starts returning a result (series) → the alert enters **pending** state.
2. If it's still true after the `for:` duration (15s here) → it transitions to **firing**.
3. Each rule stamps `labels.incident_type` (e.g. `crashloop`) directly on the alert — this is deterministic metadata Prometheus attaches, not something the agent infers later.
4. Note the `unless` clause on `DeploymentReplicaMismatch`: it's specifically designed to *not* fire while a `CrashLoopBackOff` is simultaneously active on the same namespace, so the two rules don't race and double-classify the same root cause.

Once firing, Prometheus pushes the alert to every target listed under `alerting.alertmanagers` — here, `alertmanager.monitoring.svc.cluster.local:9093`.

---

## Phase 4 — Alertmanager

Per `alertmanager-config.yaml`:
- `route.group_by: [alertname, namespace]` — alerts sharing the same alertname+namespace within the group window get batched into one notification.
- `group_wait: 5s` — waits 5s after the first alert in a new group to catch siblings.
- `group_interval: 15s` — minimum time between sending updates for an existing group.
- `repeat_interval: 1h` — if still firing, re-notify at most hourly (not really hit in these short-lived demo faults).
- The single receiver `remediation-agent` has `send_resolved: true`, so Alertmanager will **also** POST when an alert later clears (status `resolved`) — this is what eventually closes the loop in Phase 11.

Alertmanager's only job here is grouping/dedup/timing — it doesn't understand Kubernetes at all. It fires a webhook:
```
POST http://agent.agent.svc.cluster.local:8000/webhook/alertmanager
```
with a JSON payload containing `receiver`, `status`, and a list of `alerts[]` (each with `labels`, `annotations`, `fingerprint`, `startsAt`).

---

## Phase 5 — Agent Webhook Receiver

`webhook.py` `receive_alertmanager_webhook`:
1. Logs the **entire raw payload** to the audit trail (`webhook.receive`, direction `RECV`) — nothing is processed before it's recorded.
2. Iterates `payload["alerts"]`, calling `_process_alert` per alert (alerts can arrive batched).

For each alert, `_process_alert`:
1. Reads `fingerprint` (Alertmanager's stable hash of the alert's labels) and `status`.
2. **If `status == "resolved"`**: looks up an active incident with that fingerprint via `audit.find_active_by_fingerprint`. If found, flips it to `status=resolved` and logs `alert.resolved` — **no graph re-run, nothing else happens**. This is the "resolution" end of the loop described in Phase 11.
3. **If still firing and a pending incident already exists for this fingerprint**: logs `alert.duplicate` and stops — Alertmanager will keep re-POSTing the same firing alert on its group interval, so this is the dedup guard.
4. **Otherwise (new firing alert)**: calls `classify_alert(alert)` then `run_incident_pipeline(...)`.

### Secondary front door (parallel, optional, off by default)
At the same time, if `log_trigger_enabled=true`, `log_watcher.py` is independently polling Loki every `log_poll_interval_seconds` for lines matching `log_error_pattern` (e.g. `/error|panic|oomkilled/`). It:
- Collapses each matching line into a stable **signature** (numbers/IPs/hex masked) so repeated noisy lines dedupe to one incident.
- Applies a 15-minute per-signature cooldown.
- Classifies the line via diagnosis/log_classifier.py (keyword match, optionally LLM-assisted if `agentic_log_classification=true`, staying `unknown` below `log_classification_min_confidence`).
- Calls the **same** `run_incident_pipeline` as the metric path. From here on, both front doors are identical.

---

## Phase 6 — Classification (deterministic, no LLM)

diagnosis/classifier.py `classify_alert`:
- Pulls `incident_type` straight from the label Prometheus already stamped (no inference needed — the rule authored it).
- Extracts `namespace` (default `"demo"`).
- Extracts `resource_name` with incident-type-specific logic — e.g. for `crashloop` it's `labels["pod"]`; for `networkpolicy_block` it parses the `instance` URL (`http://demo-web.demo.svc...` → `demo-web`); for `replica_mismatch` it's `labels["deployment"]`.
- Returns a `ClassifiedAlert` dataclass (`alertname`, `incident_type`, `namespace`, `resource_name`, `severity`, `labels`, `annotations`).

---

## Phase 7 — Incident Creation

`run_incident_pipeline`:
1. `audit.create_incident(...)` inserts a row into the Postgres `incidents` table with `status=pending`, returns `incident_id`.
2. Logs `webhook.classify` step.
3. Builds the initial `IncidentState` dict (incident_id, type, namespace, resource_name, alert_severity, labels, annotations, raw_alert) — this is the shared "blackboard" that flows through every LangGraph node.
4. Calls `graph.ainvoke(initial_state)`.

---

## Phase 8 — The LangGraph Diagnosis/Planning Pipeline

This is a single `StateGraph` (graph/graph.py) over `IncidentState`. Nodes mutate the same shared dict; there's no agent-to-agent messaging.

**8.1 `investigate`** (nodes/investigate.py)
- Always runs `build_context()` (diagnosis/context.py) — a deterministic, incident-type-specific PromQL/LogQL fan-out. E.g. for `crashloop`:
  - `query_prometheus(...)` → `GET {prometheus_url}/api/v1/query?query=kube_pod_container_status_restarts_total{namespace="demo", pod="<pod>"}`
  - `query_loki(...)` → `GET {loki_url}/loki/api/v1/query_range?query={namespace="demo", pod="<pod>"}&limit=20&since=10m`
  - For `replica_mismatch` it pulls `spec_replicas`, `available_replicas`, **and** `unavailable_replicas` (the last is what actually drives the diagnosis, since spec-vs-available alone looks healthy during a rolling update).
  - Every single outbound query and its raw response is logged (`prometheus.query`/`loki.query`, SEND then RECV) — this becomes the evidence shown in the UI.
- **If `agentic_investigation=true`**: additionally runs a bounded ReAct agent (graph/react_investigator.py) using `ChatBedrockConverse`, which can freely call a read-only toolbelt (graph/tools.py: `prometheus_query`, `loki_logs`, `get_pod`, `describe_deployment`, `list_events`, `get_node`, `get_endpoints`, `list_networkpolicies`) up to `agent_max_steps` times, and writes a narrative summary + `evidence_trail`. This is additive — it never replaces the numeric context, and any failure falls back silently to the deterministic context alone.

**8.2 `assess_severity`** (nodes/severity.py) — pure rule-based (`heuristics.rule_based_severity`): scores P1–P4 from restart counts / replica gaps / node state. No LLM.

**8.3 `historical`** (nodes/historical.py) — looks up ≤3 similar past resolved incidents of the same type from Postgres (`audit.find_similar_resolved`), optionally blended with pgvector embedding similarity if `hybrid_retrieval_enabled`.

**8.4 `supervisor`** (nodes/supervisor.py) — routes to one of `gather_more | auto_plan | full_rca | escalate`:
- Deterministic default: P4 severity + a strong same-resource historical match → `auto_plan`, else `full_rca`.
- If `agentic_supervisor=true`: an LLM makes the call instead, but it's backstopped — `auto_plan` is only honored if a real candidate actually exists, and `gather_more` loops back to `investigate` but is hard-capped by `agent_max_steps` (shared hop budget `supervisor_hops`).

**8.5a `auto_plan`** (fast path) — replays a trusted historical plan's `action`/`steps`, but **re-locks** `target.namespace`/`target.name` to *this* incident server-side (never trusts the old record's target blindly).

**8.5b `rca` → `plan`** (full path):
- `rca` (nodes/rca.py) calls `bedrock_client.generate_diagnosis(...)` → Bedrock/Nova returns `diagnosis_text` + `confidence_score` (0.0–1.0). If `confidence < 0.4` → routes straight to `escalate`.
- `plan` (nodes/plan.py) builds the remediation plan. The **action is never free text** — it's selected from a fixed allow-list in remediation/templates.py keyed by `incident_type`. If `llm_authors_action=true`, Bedrock picks among the allowed candidate tools via a genuine Converse **toolUse call** (not JSON parsing) — each candidate action is exposed as a `toolSpec`; the LLM can only name one of them, never invent a new action/command. Output: `{title, steps, action, selected_via, target}`.

**8.6 `reflect`** (nodes/reflect.py) — if `agentic_reflection=true`, an LLM self-critiques the plan vs. the evidence (`ok` / `insufficient`); `insufficient` loops back to `investigate` (same shared hop budget). Never edits the plan itself.

**8.7 `guardrail`** (nodes/guardrail.py, graph/guardrail.py) — the deterministic safety gate, no LLM:
- Checks `plan["action"]` is in the allow-list for this `incident_type`.
- Checks the plan's target namespace/resource matches the alert's own (`expected_namespace`/`expected_resource_name`).
- FAIL → sets `escalation_reason` → routes to `escalate`.
- PASS → returns `None` (graph requirement: no-op update) → routes to `END` with the plan intact.

**8.8 `escalate`** — terminal node; sets `escalation_reason`, ends the graph with no plan to approve.

---

## Phase 9 — Persisting the Result

Back in `run_incident_pipeline`, after `graph.ainvoke` returns:
1. `audit.update_incident(...)` persists `context_snapshot`, `diagnosis_text`, `confidence_score`, `computed_severity`, `agent_evidence`, `router_decision`/`rationale`, `reflections` — regardless of outcome.
2. If `escalation_reason` is set → status becomes `escalated`, logged, and the function returns early (**no plan shown for approval**).
3. Otherwise → `remediation_plan` is persisted, logged as `remediation.plan_ready`, and the incident sits at `status=pending` waiting for a human.

---

## Phase 10 — Human Approval UI

ui/routes.py serves `http://localhost:8000`:
- `GET /` — incident queue.
- `GET /incident/{id}` — full detail: diagnosis text, PromQL/LogQL evidence, historical matches, plan title/steps/action, agentic reasoning trace.
- `POST /incident/{id}/approve` → kicks off execution (next phase) as a FastAPI `BackgroundTask`.
- `POST /incident/{id}/reject` → `status=rejected`, nothing touches the cluster, logged.
- `POST /incident/{id}/retry` / `/reprocess` — recovery paths for stuck incidents (e.g. re-run the executor, or re-run the whole diagnosis graph if the plan was never populated).

**Nothing has touched the real cluster up to this exact point.**

---

## Phase 11 — Execution (only after Approve)

`executor.py` `execute_remediation`:
1. Looks up the handler registered for `plan["action"]` (e.g. `delete_pod`, `rollout_restart_deployment`, `fix_service_selector`, `delete_blocking_networkpolicy`, `recover_node`).
2. Logs `executor.dispatch` (direction `ACTION`).
3. Loops up to `MAX_ATTEMPTS=60` times, every `POLL_INTERVAL_SECONDS=5`:
   - `attempt_fn(incident_id, namespace, resource_name)` — performs (or re-performs, idempotently) the actual Kubernetes API mutation (e.g. delete the pod, patch the deployment image back, restart the rollout).
   - `check_fn(namespace, resource_name)` — re-checks the **same underlying signal Prometheus alerts on** (available replicas, endpoint addresses, node `Ready`, etc.) — not just "did the API call succeed."
   - `on_update(status, result)` callback writes live progress into Postgres so the UI shows `in_progress` with attempt counts in real time.
4. As soon as `check_fn` reports resolved → runs an optional `on_resolved_fn` (e.g. auto-uncordon a node), sets `status=executed`, logs `executor.result`, done.
5. If the budget is exhausted without resolving → `status=execution_failed`, with a `manual_hint` if one exists (e.g. node repair sometimes needs out-of-band kubelet restart).

---

## Phase 12 — Closing the Loop

- The underlying Kubernetes objects are now healthy (e.g. pod `Running`, replicas available, endpoints populated, node `Ready`).
- Within one more scrape (≤15s) + evaluation cycle (≤15s), Prometheus's PromQL expression for that alert **stops matching** → the alert transitions out of `firing`.
- Because `send_resolved: true` is set, Alertmanager sends a second webhook with `status: "resolved"` for that same `fingerprint`.
- Back in Phase 5 step 2: the agent finds the still-active incident by fingerprint and flips it to `status=resolved` — completing the full circle from "fault happens" to "system itself confirms via the same telemetry path that it's fixed."

---

### One-paragraph summary of the whole chain
Fault → metrics (kube-state-metrics/node-exporter/blackbox) scraped by Prometheus every 15s → PromQL rule evaluated every 15s, alert goes pending→firing after its `for:` window → Alertmanager groups/dedupes and POSTs a webhook → agent logs the raw payload, dedupes by fingerprint, classifies via label → creates a Postgres incident row → runs the LangGraph pipeline (deterministic PromQL/LogQL evidence fan-out + optional ReAct tool loop → rule-based severity → historical lookup → supervisor routing → RCA via Bedrock with a confidence gate → plan via Bedrock toolUse constrained to a fixed action allow-list → optional self-reflection loop → deterministic guardrail re-validating action+target) → persists diagnosis/plan and surfaces it in the Approval UI → human approves → executor performs the real Kubernetes mutation and polls the exact same signal Prometheus alerts on until it clears or the retry budget is exhausted → Prometheus observes the fix within its next scrape/eval cycle → Alertmanager sends a `resolved` webhook → agent closes the incident.