# Instructions: Running and Verifying the Remediation Agent

This is a step-by-step guide to stand up the whole system (cluster + monitoring
+ agent + fault injectors), run one incident end-to-end, and verify that every
stage of the pipeline actually did what it claims to have done.

Everything referenced below lives under this `project/` folder. See
[plan.md](plan.md) for the design rationale and [diagram.md](diagram.md) for
the architecture diagrams.

---

## 1. Prerequisites

Install these on your machine:

| Tool | Why | Check |
|------|-----|-------|
| Docker Desktop (or Docker Engine) | runs `kind` nodes as containers | `docker version` |
| [kind](https://kind.sigs.k8s.io/) | local Kubernetes cluster | `kind version` |
| kubectl | talk to the cluster | `kubectl version --client` |
| Python 3.11+ | run the agent locally / injector scripts | `python --version` |
| An AWS account with **Amazon Bedrock** access | diagnosis text generation | see step 3 |
| `make` (optional) | convenience wrapper around the commands below | `make --version` |

`make` is optional - every target in [Makefile](Makefile) is a thin wrapper
around plain `kubectl`/`docker`/`kind`/`python` commands. If you're on Windows
without WSL/Git Bash, just run the underlying command shown in each Makefile
target directly in PowerShell.

---

| Tool | Why | Check |
|------|-----|-------|
| Docker Desktop (or Docker Engine) | runs `kind` nodes as containers | `docker version` |
| [kind](https://kind.sigs.k8s.io/) | local Kubernetes cluster | `kind version` |
| kubectl | talk to the cluster | `kubectl version --client` |
| Python 3.11+ (with the `kubernetes` package) | injector scripts | `python --version` |
| An AWS account with **Amazon Bedrock** access | diagnosis text generation | see step 2 |

On Windows, use the PowerShell scripts under [scripts/](scripts) (this is the
primary, tested path). On macOS/Linux, [Makefile](Makefile) wraps the same
commands via `make`.

---

## 2. Configure AWS Bedrock (Amazon Nova) credentials via .env

The agent uses **Amazon Nova** models (`amazon.nova-lite-v1:0` by default,
via the Bedrock **Converse API**) purely to turn metrics/logs into a
human-readable diagnosis paragraph - it never decides the remediation action.

1. In the AWS Console, go to **Amazon Bedrock -> Model access** in your chosen
   region (default `us-east-1`) and request/enable access to the **Nova**
   models (Nova Micro / Nova Lite / Nova Pro). Approval is usually instant.
2. Create an IAM user/role with a policy allowing at least:
   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Effect": "Allow",
       "Action": ["bedrock:InvokeModel", "bedrock:Converse"],
       "Resource": "*"
     }]
   }
   ```
3. Get an access key + secret key (or temporary STS credentials) for that
   principal.
4. Create your local `.env` file - it is git-ignored and is the **only**
   place your AWS credentials live on disk:
   ```powershell
   cd project
   Copy-Item .env.example .env
   notepad .env   # fill in AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (and AWS_SESSION_TOKEN if using STS)
   ```

`deploy.ps1` (or `make agent-secret`) reads this file directly with
`kubectl create secret generic ... --from-env-file=.env` - your keys never
appear in a command line, shell history, or any committed file.

If you'd rather skip Bedrock entirely, leave the AWS keys blank and set
`BEDROCK_ENABLED: "false"` in [agent/k8s/deployment.yaml](agent/k8s/deployment.yaml) -
the agent will use a clearly-labelled fallback diagnosis string instead, so
the rest of the pipeline still works for a demo.

---

## 3. Deploy everything with one command

```powershell
cd project
./scripts/deploy.ps1
```

This single script (idempotent - safe to re-run) does all of the following,
printing each step as it runs and stopping immediately if anything fails:

1. Checks `kind`/`kubectl`/`docker`/`python` are installed and `.env` exists.
2. Creates the kind cluster (1 control-plane + 2 workers) if it doesn't already exist.
3. Applies the `demo`/`monitoring`/`agent` namespaces and the demo workloads.
4. Applies Prometheus, Alertmanager, kube-state-metrics, node-exporter,
   blackbox-exporter, Loki and Promtail, then waits for all of them to
   become Ready.
5. Builds the agent's Docker image and loads it into the kind cluster (no registry needed).
6. Applies the agent's RBAC (ServiceAccount + ClusterRole/Binding).
7. Creates/updates the `aws-bedrock-credentials` Secret from your `.env` file.
8. Deploys the agent and waits for it to become Ready.

On macOS/Linux, the equivalent is:
```bash
make cluster-up && make workloads && make monitoring && make deploy-all
```
(`make agent-secret` reads the same `.env` file via `--from-env-file`.)

If you only changed agent code and want to skip re-creating the cluster:
`./scripts/deploy.ps1 -SkipClusterCreate`.

---

## 4. Verify the deployment

```powershell
./scripts/status.ps1
```

This prints node status, pod status in all three namespaces, an agent
`/healthz` check, and the current audit log contents. You should see:

- `kubectl get nodes` -> 3 nodes, all `Ready`.
- Pods in `demo`, `monitoring`, and `agent` namespaces all `Running`/`Ready`.
- `/healthz` -> `{"status":"ok"}`.

Also open in a browser:
- http://localhost:8000 - Approval UI (empty **Incident Queue** to start).
- http://localhost:9090/targets - all scrape targets `UP`.
- http://localhost:9090/alerts - the 5 MVP alert rules listed, all `Inactive`.



## 5. Run one incident end-to-end (Phase 1: CrashLoopBackOff)

**Inject the fault:**

```powershell
./scripts/inject.ps1 -Incident crashloop
```

This patches the `demo-web` Deployment with a command that exits immediately,
after stashing the original command in an annotation. Watch it happen:

```bash
kubectl get pods -n demo -l app=demo-web -w
# STATUS should cycle: Running -> Error -> CrashLoopBackOff
```

**Watch the agent's own log** in another terminal - this is the "what is it
sending/receiving at each step" view in real time:

```bash
kubectl logs -n agent deploy/agent -f
```

Within ~30-90 seconds (Prometheus evaluates the rule every 15s with a 30s
`for:` window, then Alertmanager groups for up to 10s) you should see, in
order:

1. `step=webhook.receive <-- RECV` - the raw Alertmanager webhook JSON.
2. `step=webhook.classify --- INFO` - incident_type=`crashloop`, resource=pod name.
3. `step=prometheus.query --> SEND` / `<-- RECV` - the restart-count PromQL query and result.
4. `step=loki.query --> SEND` / `<-- RECV` - the LogQL query for that pod's recent logs.
5. `step=bedrock.converse --> SEND` - the exact request sent to Amazon Nova (system prompt + user prompt with the alert + context).
6. `step=bedrock.converse <-- RECV` - Nova's diagnosis text.
7. `step=remediation.plan_ready --- INFO` - the fixed template plan (`action=delete_pod`).

**Verify in the UI:** refresh http://localhost:8000 - an incident with status
`pending` should appear. Click into it to see, in order: the raw alert, the
Prometheus/Loki context, the Bedrock diagnosis, the proposed plan, and a full
timeline of every SEND/RECV event above (same information as the terminal
log, rendered for a human).

**Approve it:**

Click **Approve** in the UI (or `curl -X POST http://localhost:8000/incident/<id>/approve`).

The agent log will now show:

8. `step=executor.dispatch ==> ACTION` - "human approved, executing now".
9. `step=k8s.patch_deployment --> SEND` / `<-- RECV` - reverting the Deployment's container command back to the original (read from the annotation the injector stashed).
10. `step=k8s.delete_pod --> SEND` / `<-- RECV` - deleting the crash-looping pod so a fresh, fixed replica gets scheduled.
11. `step=executor.result <-- RECV` - final JSON result (`status: executed`).

**Verify the fix actually worked:**

```bash
kubectl get pods -n demo -l app=demo-web -w      # new pod reaches Running/Ready, restarts stop climbing
```

Open http://localhost:9090/alerts - `PodCrashLoopBackOff` should go from
`firing` to `inactive` within a couple of evaluation cycles. When it resolves,
Alertmanager sends a `resolved` webhook and the agent log shows
`step=alert.resolved --- INFO`; the incident's status flips to `resolved` in
the UI.

**Verify the audit trail:**

```powershell
./scripts/status.ps1
```

You should see one row: `incident_type=crashloop`, `status=resolved` (or
`executed` if the alert hasn't re-evaluated yet), `decision_by=human`.

To try a **Reject** instead, re-run the injector and click **Reject** in the
UI - the agent log will show `step=ui.decision ==> ACTION {"decision":
"reject", ...}` and no `k8s.*` calls will ever be made; the pod stays broken
until you manually run `./scripts/inject.ps1 -Incident crashloop -Revert`.

---

## 6. Run the remaining MVP incidents (Phase 2)

Each follows the same pattern: inject -> watch the agent log -> approve in
the UI -> verify the fix -> confirm the alert clears.

| # | Inject | What it breaks | Watch for the fix |
|---|--------|-----------------|--------------------|
| 2 | `./scripts/inject.ps1 -Incident node` | stops kubelet inside a kind worker container via `docker exec` | run `./scripts/inject.ps1 -Incident node -Revert` (restarts kubelet out-of-band) *before or right after* clicking Approve; the agent cordons the node and polls for up to 30s - if it sees `Ready` in that window it uncordons automatically, otherwise run `kubectl uncordon <node>` yourself once it recovers |
| 3 | `./scripts/inject.ps1 -Incident service` | breaks `demo-web` Service's selector | `kubectl get endpoints -n demo demo-web -w` regains addresses after `k8s.patch_service` |
| 4 | `./scripts/inject.ps1 -Incident networkpolicy` | applies a deny-all-ingress NetworkPolicy to demo-web | `NetworkPolicyBlockingTraffic` alert clears after `k8s.delete_networkpolicy` |
| 5 | `./scripts/inject.ps1 -Incident replica` | sets `demo-api`'s image to a nonexistent tag | `kubectl get deployment -n demo demo-api -w` reaches full availability after `k8s.patch_deployment` restores the image and triggers a rollout restart |

Each also supports `-Revert` (e.g. `./scripts/inject.ps1 -Incident node -Revert`)
to manually undo a fault **without** going through the agent, useful if you
want to abandon a demo run.

On macOS/Linux: `make inject-<name>` / `make revert-<name>` wrap all five (see [Makefile](Makefile)).

> **Note on incident #2 (Node NotReady):** a node whose own kubelet is down
> cannot run a Kubernetes-scheduled fix - nothing can start on that node,
> including a "restart kubelet" job, until kubelet is already back. So the
> agent's remediation is limited to cordon (safe, via the API) + wait/report;
> the actual kubelet restart is inherently out-of-band (`docker exec` in this
> kind demo, node auto-repair/cluster autoscaler in a real cloud cluster).
> If you cordon a node and then abandon the demo without reverting, run
> `kubectl uncordon <node>` manually to clean up.

---

## 7. Running the agent locally instead of in-cluster (optional)

Useful for iterating on agent code without rebuilding the image each time:

```bash
kubectl port-forward -n monitoring svc/prometheus 9090:9090 &
kubectl port-forward -n monitoring svc/loki 3100:3100 &

cd agent
python -m venv .venv && . .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -r requirements.txt

export PROMETHEUS_URL=http://localhost:9090
export LOKI_URL=http://localhost:3100
export KUBE_IN_CLUSTER=false   # uses your local kubeconfig instead of in-cluster SA
export AUDIT_DB_PATH=./audit.db

# Reuse the same .env you created in step 2 instead of re-typing credentials:
cp ../.env .env   # pydantic-settings (see app/config.py) loads .env automatically

uvicorn app.main:app --host 0.0.0.0 --port 8000
```

You'll also need Alertmanager to be able to reach your laptop instead of the
in-cluster `agent` Service - edit
[monitoring/prometheus/alertmanager-config.yaml](monitoring/prometheus/alertmanager-config.yaml)'s
`webhook_configs.url` to point at an address reachable from inside kind (e.g.
`http://host.docker.internal:8000/webhook/alertmanager` on Docker
Desktop), then re-apply it and restart Alertmanager.

---

## 8. Troubleshooting

- **Alert never fires**: check http://localhost:9090/targets - if a scrape
  target is `DOWN`, the underlying metric never gets populated. Check
  `kubectl get pods -n monitoring` for crashing exporters.
- **Alertmanager isn't calling the agent**: `kubectl logs -n monitoring
  deploy/alertmanager` and check the webhook URL resolves
  (`agent.agent.svc.cluster.local` only resolves from inside the cluster).
- **Bedrock call fails**: the agent logs the exact error and falls back to a
  clearly-labelled fallback diagnosis string, so the demo still completes -
  common causes are model access not enabled for your account/region, or an
  IAM policy missing `bedrock:Converse`.
- **`kind load docker-image` succeeds but the pod still can't pull**: make
  sure `imagePullPolicy: IfNotPresent` (already set in
  [agent/k8s/deployment.yaml](agent/k8s/deployment.yaml)) and that the image
  tag matches exactly (`capstart/remediation-agent:local`).
- **Privileged Job for node recovery never completes**: some Docker/kind
  setups restrict privileged containers; check
  `kubectl get jobs -n kube-system` and `kubectl logs -n kube-system
  job/kubelet-restart-...` for details.

---

## 9. Cleanup

```powershell
./scripts/teardown.ps1
```

(macOS/Linux: `make clean`, or plain `kind delete cluster --name capstart`).
This removes everything - there is no state kept outside the kind cluster's
containers (the SQLite audit log lives in an `emptyDir` volume in the agent pod).
