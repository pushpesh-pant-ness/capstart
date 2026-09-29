"""Shared Kubernetes client bootstrap, used by both the executor and the
read-only cluster-status dashboard so there is exactly one place that decides
in-cluster vs local kubeconfig."""
from __future__ import annotations

from kubernetes import client
from kubernetes import config as kube_config

from .config import settings

_initialized = False


def init_kube() -> None:
    global _initialized
    if _initialized:
        return
    if settings.kube_in_cluster:
        kube_config.load_incluster_config()
    else:
        kube_config.load_kube_config()
    _initialized = True


def core_v1() -> client.CoreV1Api:
    init_kube()
    return client.CoreV1Api()


def apps_v1() -> client.AppsV1Api:
    init_kube()
    return client.AppsV1Api()


def networking_v1() -> client.NetworkingV1Api:
    init_kube()
    return client.NetworkingV1Api()


def batch_v1() -> client.BatchV1Api:
    init_kube()
    return client.BatchV1Api()
