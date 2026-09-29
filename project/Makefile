# Convenience targets for the capstart remediation-agent demo.
# Requires: kind, kubectl, docker, python3 (with kubernetes client for injector/).
# On Windows, prefer the PowerShell scripts under scripts/ instead (deploy.ps1,
# teardown.ps1, inject.ps1, status.ps1) - see instructions.md.
#
# AWS credentials come from a git-ignored .env file: copy .env.example to
# .env and fill in your values before running `make agent-secret` / `deploy-all`.

CLUSTER_NAME := capstart
IMAGE := capstart/remediation-agent:local

.PHONY: cluster-up cluster-down namespaces workloads monitoring \
        agent-image agent-load agent-rbac agent-secret agent-deploy deploy-all \
        logs-agent audit-list \
        inject-crashloop inject-node inject-service inject-networkpolicy inject-replica \
        revert-crashloop revert-node revert-service revert-networkpolicy revert-replica \
        clean

## --- Cluster lifecycle -------------------------------------------------

cluster-up:
	kind create cluster --config cluster/kind-config.yaml

cluster-down:
	kind delete cluster --name $(CLUSTER_NAME)

## --- Deploy everything --------------------------------------------------

namespaces:
	kubectl apply -f cluster/workloads/namespaces.yaml

workloads: namespaces
	kubectl apply -f cluster/workloads/demo-app.yaml

monitoring: namespaces
	kubectl apply -f monitoring/prometheus/prometheus-config.yaml
	kubectl apply -f monitoring/prometheus/prometheus-deployment.yaml
	kubectl apply -f monitoring/prometheus/alertmanager-config.yaml
	kubectl apply -f monitoring/prometheus/alertmanager-deployment.yaml
	kubectl apply -f monitoring/kube-state-metrics/kube-state-metrics.yaml
	kubectl apply -f monitoring/node-exporter/node-exporter.yaml
	kubectl apply -f monitoring/blackbox/blackbox-exporter.yaml
	kubectl apply -f monitoring/loki/loki-config.yaml
	kubectl apply -f monitoring/loki/loki-deployment.yaml
	kubectl apply -f monitoring/loki/promtail-config.yaml
	kubectl apply -f monitoring/loki/promtail-daemonset.yaml

agent-image:
	docker build -t $(IMAGE) agent/

agent-load: agent-image
	kind load docker-image $(IMAGE) --name $(CLUSTER_NAME)

agent-rbac: namespaces
	kubectl apply -f agent/k8s/rbac.yaml

# Reads AWS credentials from a git-ignored .env file (copy .env.example -> .env
# and fill it in first). Never commits real credentials.
agent-secret: namespaces
	kubectl create secret generic aws-bedrock-credentials -n agent \
		--from-env-file=.env \
		--dry-run=client -o yaml | kubectl apply -f -

agent-deploy: agent-load agent-rbac
	kubectl apply -f agent/k8s/deployment.yaml

deploy-all: workloads monitoring agent-secret agent-deploy
	@echo "Everything deployed. UI: http://localhost:8000  Prometheus: http://localhost:9090"

## --- Operate --------------------------------------------------------

logs-agent:
	kubectl logs -n agent deploy/agent -f

audit-list:
	kubectl exec -n agent deploy/agent -- python -c \
		"import sqlite3; c=sqlite3.connect('/data/audit.db'); c.row_factory=sqlite3.Row; \
		 [print(dict(r)) for r in c.execute('select id,incident_type,status,received_at from incidents order by id')]"

## --- Fault injection (see injector/) ---------------------------------

inject-crashloop:
	python injector/crashloop.py

inject-node:
	python injector/node_not_ready.py

inject-service:
	python injector/service_unreachable.py

inject-networkpolicy:
	python injector/networkpolicy_block.py

inject-replica:
	python injector/replica_mismatch.py

revert-crashloop:
	python injector/crashloop.py --revert

revert-node:
	python injector/node_not_ready.py --revert

revert-service:
	python injector/service_unreachable.py --revert

revert-networkpolicy:
	python injector/networkpolicy_block.py --revert

revert-replica:
	python injector/replica_mismatch.py --revert

## --- Cleanup ----------------------------------------------------------

clean: cluster-down
