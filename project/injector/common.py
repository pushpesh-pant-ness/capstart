"""
Shared helpers for the fault-injection scripts.

These scripts are standalone and deliberately separate from the remediation
agent (see plan.md's Non-Goals) - a human runs them directly, or via
`make inject-<name>`, to simulate real-world failures against the kind
cluster using your local kubeconfig.
"""
from __future__ import annotations

from kubernetes import client, config


def load_client() -> None:
    config.load_kube_config()


def core_v1() -> client.CoreV1Api:
    load_client()
    return client.CoreV1Api()


def apps_v1() -> client.AppsV1Api:
    load_client()
    return client.AppsV1Api()


def networking_v1() -> client.NetworkingV1Api:
    load_client()
    return client.NetworkingV1Api()


def announce(action: str, detail: str) -> None:
    print(f"[injector] {action}: {detail}")
