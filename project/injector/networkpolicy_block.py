"""
Incident #4: NetworkPolicy blocking pod/node traffic.

Applies a deny-all-ingress NetworkPolicy against the demo-web pods, labelled
chaos=injected so the agent's executor knows it is safe to delete. This also
blocks blackbox-exporter's synthetic probe traffic, which is what fires the
NetworkPolicyBlockingTraffic alert.

Usage:
  python injector/networkpolicy_block.py
  python injector/networkpolicy_block.py --revert
"""
from __future__ import annotations

import argparse

from kubernetes import client
from kubernetes.client.rest import ApiException

from common import networking_v1

NAMESPACE = "demo"
POLICY_NAME = "chaos-deny-demo-web"


def inject() -> None:
    net = networking_v1()
    policy = client.V1NetworkPolicy(
        metadata=client.V1ObjectMeta(name=POLICY_NAME, namespace=NAMESPACE, labels={"chaos": "injected"}),
        spec=client.V1NetworkPolicySpec(
            pod_selector=client.V1LabelSelector(match_labels={"app": "demo-web"}),
            policy_types=["Ingress"],
            ingress=[],
        ),
    )
    net.create_namespaced_network_policy(NAMESPACE, policy)
    print(f"Fault injected: NetworkPolicy '{POLICY_NAME}' now denies all ingress to demo-web.")
    print("Watch: Prometheus alert 'NetworkPolicyBlockingTraffic' (http://localhost:9090/alerts)")


def revert() -> None:
    net = networking_v1()
    try:
        net.delete_namespaced_network_policy(POLICY_NAME, NAMESPACE)
        print(f"Reverted: NetworkPolicy '{POLICY_NAME}' deleted manually (bypassing the agent).")
    except ApiException as exc:
        if exc.status == 404:
            print("NetworkPolicy already gone - nothing to revert.")
        else:
            raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revert", action="store_true")
    args = parser.parse_args()
    revert() if args.revert else inject()
