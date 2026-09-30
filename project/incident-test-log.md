# Incident test log

Simple record of the 5 MVP fault injections run end-to-end against the live
demo cluster: what was injected, what it broke, and exactly how the agent
detected and fixed it. See [instructions.md](instructions.md) for how to
reproduce any of these.

---

## 1. Crash loop (`injector/crashloop.py`)

- **Injected:** patched `demo-web`'s container command to `sh -c "echo boom; exit 1"`,
  stashing the original command in a Deployment annotation.
- **What it did:** every new `demo-web` pod immediately crashed and restarted,
  climbing the restart counter (`CrashLoopBackOff`).
- **Detected by:** `PodCrashLoopBackOff` alert
  (`increase(kube_pod_container_status_restarts_total[5m]) > 3`).
- **Agent's fix:** queried the restart count + recent Loki logs, got a Bedrock
  diagnosis, proposed `delete_pod`. On approval it deleted the crashing pod
  (the ReplicaSet recreates it with the healthy image/command once the
  Deployment's command annotation is restored).
- **Result:** new pod came up `Running`, restarts stopped, alert cleared.

## 2. Service unreachable (`injector/service_unreachable.py`)

- **Injected:** broke the `demo-web` Service's selector so it no longer
  matched any pod labels.
- **What it did:** the Service's Endpoints object emptied out (zero backing
  addresses) - a full outage for anything calling `demo-web`.
- **Detected by:** `ServiceEndpointsUnreachable` alert
  (`kube_endpoint_address_available == 0`).
- **Agent's fix:** action `fix_service_selector` - restored the Service's
  selector to the last-known-good value via `k8s.patch_service`.
- **Result:** Endpoints repopulated with both pod IPs immediately.

## 3. NetworkPolicy blocking traffic (`injector/networkpolicy_block.py`)

- **Injected:** applied a deny-all-ingress `NetworkPolicy` (labelled
  `chaos=injected`) to `demo-web`.
- **What it did:** a synthetic blackbox-exporter probe to `demo-web` started
  failing (connections blocked).
- **Detected by:** `NetworkPolicyBlockingTraffic` alert (`probe_success == 0`).
- **Agent's fix:** action `delete_blocking_networkpolicy` - listed
  NetworkPolicies in the namespace and deleted the one tagged
  `chaos=injected` via `k8s.delete_networkpolicy`.
- **Result:** probe succeeded again, alert cleared.

## 4. Replica mismatch (`injector/replica_mismatch.py`)

- **Injected:** set `demo-api`'s image to a nonexistent tag
  (`nginx:this-tag-does-not-exist`).
- **What it did:** the new (surge) replica got stuck in `ImagePullBackOff` -
  the Deployment never reached full availability.
- **Detected by:** `DeploymentReplicaMismatch` alert.
- **Agent's fix:** action `rollout_restart_deployment` - restored the
  last-known-good image and triggered a fresh rollout via
  `k8s.patch_deployment`.
- **Result:** both replicas back on `nginx:1.25-alpine` and `Available`.

## 5. Node NotReady (`injector/node_not_ready.py`)

- **Injected:** `docker exec <node> systemctl stop kubelet` on a kind worker
  node.
- **What it did:** the node stopped reporting heartbeats and flipped to
  `NotReady` after Kubernetes' node-monitor-grace-period (~40-50s).
- **Detected by:** `NodeNotReady` alert (`kube_node_status_condition == 0`).
- **Agent's fix:** action `cordon_and_wait` - cordoned the node
  (`k8s.cordon_node`) so nothing new schedules there, then polled every 5s
  (up to 5 minutes) waiting for the node to report `Ready` again, since a
  downed kubelet can't be fixed through the Kubernetes API itself. Once the
  kubelet was restarted (out-of-band, `systemctl start kubelet`) and the node
  came back `Ready`, the agent auto-uncordoned it (`k8s.uncordon_node`).
- **Result:** node back to schedulable `Ready`, no manual `kubectl uncordon`
  needed.
- **Note:** while the node was down, `demo-web`'s pods (scheduled there via
  `nodeSelector`) also tripped `service_unreachable` / `replica_mismatch` /
  `networkpolicy_block` alerts as a knock-on effect - not separate bugs, all
  self-resolved once the node recovered.

---

## Bugs found and fixed while testing

1. **`DeploymentReplicaMismatch` never fired.** It originally compared
   `spec_replicas != status_replicas_available`, but under a Deployment's
   default RollingUpdate strategy the old replicas stay `Available` the whole
   time - only the new surge replica fails - so the two sides were always
   equal. Changed the rule to fire on
   `kube_deployment_status_replicas_unavailable > 0` instead, and added that
   same metric to the agent's diagnosis context so Bedrock stops seeing a
   "healthy-looking" spec/available pair and second-guessing a real alert.
2. **That fix then over-fired during crash loops.** A crashing pod is also
   briefly "unavailable", so `DeploymentReplicaMismatch` started racing
   `PodCrashLoopBackOff` and sometimes misclassified a crash loop as a
   replica mismatch (wrong remediation action). Fixed by excluding namespaces
   with an active `CrashLoopBackOff` pod from the `DeploymentReplicaMismatch`
   expression.
3. **`deploy.ps1` silently ran stale agent code.** Rebuilding the Docker image
   with the same `:local` tag didn't change the Deployment spec, so
   `kubectl apply` never restarted the pod. Added an explicit
   `kubectl rollout restart deployment/agent` step after every image rebuild.
