# Plan: Post-Deployment Kubernetes Incident Remediation Agent

## Problem Statement

Modern Kubernetes deployments frequently experience operational incidents after
release — nodes going down, pods losing connectivity to other nodes, or
services becoming unreachable — that require fast diagnosis and remediation.
Manual triage is slow and error-prone, but fully automated remediation is
risky without human oversight.

This project builds a **lightweight, human-in-the-loop remediation agent**
for a small Kubernetes cluster. Incidents are deliberately injected (for
demo/testing purposes) into a 3-node `kind` cluster running minimal demo
workloads. Prometheus detects the resulting anomalies and notifies the agent
via Alertmanager webhooks. The agent:

1. **Understands the problem** — classifies the incident type from alert
   labels, pulls supporting context (Prometheus metrics + Loki logs), and
   generates a human-readable diagnosis using AWS Bedrock.
2. **Proposes a solution** — drafts a fixed, deterministic remediation action
   template appropriate to the incident type (not LLM-generated actions, to
   keep execution safe and predictable).
3. **Requires human approval** — every proposed remediation is shown in a
   minimal web UI and **must** be explicitly approved or rejected by a human
   before anything is executed. There is no auto-remediation path.
4. **Acts only after approval** — on approval, the agent executes the
   remediation directly via the Kubernetes API and records the outcome. On
   rejection, no action is taken.

All actions are recorded in an audit log (incident, diagnosis, proposed
plan, human decision, execution result, timestamps).

## Goals

- Demonstrate an end-to-end alert -> diagnose -> propose -> approve -> act
  loop for realistic Kubernetes failure scenarios.
- Keep every component lightweight (low CPU/memory), suitable for running on
  a single laptop/small VM via `kind`.
- Keep human approval as a hard requirement for every remediation, with a
  full audit trail.
- Cover a small set of well-understood incident types first (MVP), with a
  clear path to add more later.

## Non-Goals

- No auto-remediation without human approval, ever.
- No multi-tenant or multi-cluster support.
- No heavyweight dashboards (Grafana) required for MVP.
- No custom CNI — use `kind`'s default CNI (kindnet); only manipulate
  higher-level objects (NetworkPolicy, Services, Deployments, nodes).
- No LLM-generated remediation *actions* — Bedrock is used only to generate
  human-readable explanation/diagnosis text; the actual remediation logic is
  fixed, deterministic rule-based templates.

## Incident Catalog

### MVP (build first, in this order)

| # | Incident | Detection Signal (Prometheus) | Remediation Action |
|---|----------|-------------------------------|---------------------|
| 1 | Pod CrashLoopBackOff | `kube_pod_container_status_restarts_total` rate spike | Inspect logs (Loki), suggest rollback to previous image/config, or delete pod to force reschedule |
| 2 | Node NotReady (kubelet down) | `kube_node_status_condition{condition="Ready"}` == 0 | Restart kubelet/node (simulated restart in kind), cordon/uncordon as needed |
| 3 | Service/Endpoints unreachable | `kube_service` has zero endpoints / probe failure | Recreate Service/Endpoints, verify selector match |
| 4 | NetworkPolicy blocking pod/node traffic | Synthetic probe (blackbox exporter) fails between pods/nodes | Diagnose offending NetworkPolicy, suggest removal/patch |
| 5 | Deployment replica mismatch | `kube_deployment_spec_replicas` != `kube_deployment_status_available_replicas` | Suggest `rollout restart` or scale back to desired replica count |

### Stretch (add after MVP is solid)

| # | Incident | Detection Signal | Remediation Action |
|---|----------|-------------------|---------------------|
| 6 | OOMKilled pod | `kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}` | Suggest raising memory limit/request, restart |
| 7 | Pod stuck Pending (unschedulable) | `kube_pod_status_phase{phase="Pending"}` | Diagnose taints/resource requests, suggest adjustment |
| 8 | Node disk pressure / high resource usage | `node_filesystem_avail_bytes`, `container_memory_usage_bytes` thresholds | Suggest pod eviction, log rotation, or cordon node |
| 9 | ConfigMap/Secret misconfiguration | `CreateContainerConfigError` events | Suggest diff against last-known-good config, rollback |
| 10 | API server / control-plane latency spike | `apiserver_request_duration_seconds` | Suggest reducing load / restarting misbehaving controller pod |

## Architecture

See [diagram.md](diagram.md) for the full architecture and sequence
diagrams. Summary of chosen stack:

- **Cluster**: `kind`, 1 control-plane + 2 workers (3 nodes total), tiny
  alpine/busybox-based demo workloads (<50m CPU / <64Mi memory requests)
- **Metrics**: Prometheus + Alertmanager + kube-state-metrics + node-exporter
- **Logs**: Loki (single-binary, filesystem storage, short retention) +
  Promtail (DaemonSet)
- **Agent**: Python (FastAPI)
  - Rule engine: alert labels -> incident type + fixed remediation template
  - AWS Bedrock: generates the human-readable diagnosis/explanation text only
  - Executor: `kubernetes` Python client, invoked only after approval
  - Audit log: SQLite
- **Approval UI**: Minimal FastAPI + HTML/HTMX page (Approve/Reject per
  pending incident)
- **Fault injection**: standalone scripts under `injector/`, triggered
  manually by a human/Makefile target — intentionally separate from the
  agent so injected faults are never confused with agent-caused state

## Repo Structure

```
capstart/
├── cluster/                  # kind config, node/pod manifests for demo workload
│   ├── kind-config.yaml
│   └── workloads/
├── monitoring/
│   ├── prometheus/           # Prometheus + Alertmanager config, alert rules
│   ├── loki/                 # Loki + Promtail lightweight config
│   └── kube-state-metrics/
├── agent/                    # FastAPI remediation agent
│   ├── app/
│   │   ├── main.py
│   │   ├── webhook.py
│   │   ├── diagnosis/
│   │   ├── bedrock_client.py
│   │   ├── remediation/
│   │   ├── executor.py
│   │   ├── audit.py
│   │   └── ui/
│   ├── requirements.txt
│   └── Dockerfile
├── injector/                 # fault injection scripts, one per incident type
├── diagram.md
├── plan.md
└── Makefile
```

## Phased Rollout

- **Phase 0 — Foundation**: `kind` cluster + demo workload + Prometheus/
  Alertmanager/kube-state-metrics/node-exporter + Loki/Promtail, all
  lightweight configs.
- **Phase 1 — Pipeline proof**: implement Pod CrashLoopBackOff end-to-end
  (alert -> webhook -> diagnosis -> Bedrock explanation -> audit record ->
  approval UI -> approve -> executor fixes it -> alert clears). Validates
  the full loop before scaling out.
- **Phase 2 — Remaining MVP incidents**: Node NotReady, Service/Endpoints
  unreachable, NetworkPolicy block, Deployment replica mismatch.
- **Phase 3 — Polish**: show Loki log snippets alongside metrics in the UI,
  add an audit log viewer page, write a demo script/README.
- **Phase 4 — Stretch**: OOMKilled, Pending/unschedulable, disk pressure,
  ConfigMap misconfig, control-plane latency.

## Success Criteria

For each MVP incident: inject it -> Prometheus alert fires within its
`for:` window -> agent receives webhook -> diagnosis + plan appears in UI
within a few seconds -> human clicks Approve -> agent applies fix via K8s
API -> alert clears -> audit log has the full trail (detection, diagnosis,
decision, execution result).
