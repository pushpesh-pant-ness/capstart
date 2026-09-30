"""
Rule engine step 2: pull supporting evidence for a classified incident.

Every outbound query and its response is logged via log_step so you can see
*exactly* what PromQL/LogQL was sent to Prometheus/Loki and what came back -
this context is what gets handed to Bedrock and shown in the Approval UI.
"""
from __future__ import annotations

from typing import Any

import httpx

from ..config import settings
from ..logging_utils import log_step
from ..observability import traceable
from .classifier import ClassifiedAlert


def query_prometheus(incident_id: int | None, promql: str) -> dict[str, Any]:
    url = f"{settings.prometheus_url}/api/v1/query"
    log_step(incident_id, "prometheus.query", "SEND", {"url": url, "query": promql})
    try:
        resp = httpx.get(url, params={"query": promql}, timeout=settings.http_timeout_seconds)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 - want to surface any failure into the log/UI
        data = {"status": "error", "error": str(exc)}
    log_step(incident_id, "prometheus.query", "RECV", data)
    return data


def query_loki(incident_id: int | None, logql: str, limit: int = 20) -> dict[str, Any]:
    url = f"{settings.loki_url}/loki/api/v1/query_range"
    params = {"query": logql, "limit": limit, "since": "10m"}
    log_step(incident_id, "loki.query", "SEND", {"url": url, **params})
    try:
        resp = httpx.get(url, params=params, timeout=settings.http_timeout_seconds)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        data = {"status": "error", "error": str(exc)}
    log_step(incident_id, "loki.query", "RECV", data)
    return data


@traceable(name="build_context", run_type="retriever")
def build_context(incident_id: int, classified: ClassifiedAlert) -> dict[str, Any]:
    """Incident-type-specific PromQL/LogQL fan-out, returned as one JSON blob."""
    ns = classified.namespace
    name = classified.resource_name
    context: dict[str, Any] = {"incident_type": classified.incident_type}

    if classified.incident_type == "crashloop":
        context["restarts"] = query_prometheus(
            incident_id,
            f'kube_pod_container_status_restarts_total{{namespace="{ns}", pod="{name}"}}',
        )
        context["logs"] = query_loki(incident_id, f'{{namespace="{ns}", pod="{name}"}}')

    elif classified.incident_type == "node_not_ready":
        context["node_condition"] = query_prometheus(
            incident_id, f'kube_node_status_condition{{node="{name}", condition="Ready"}}'
        )

    elif classified.incident_type == "service_unreachable":
        context["endpoint_availability"] = query_prometheus(
            incident_id,
            f'kube_endpoint_address_available{{namespace="{ns}", endpoint="{name}"}}',
        )

    elif classified.incident_type == "networkpolicy_block":
        instance = classified.labels.get("instance", "")
        context["probe_result"] = query_prometheus(
            incident_id, f'probe_success{{instance="{instance}"}}'
        )

    elif classified.incident_type == "replica_mismatch":
        context["spec_replicas"] = query_prometheus(
            incident_id, f'kube_deployment_spec_replicas{{namespace="{ns}", deployment="{name}"}}'
        )
        context["available_replicas"] = query_prometheus(
            incident_id,
            f'kube_deployment_status_replicas_available{{namespace="{ns}", deployment="{name}"}}',
        )
        # spec vs. available alone looks "healthy" under RollingUpdate (old replicas stay
        # Available while only the surge replica fails) - unavailable_replicas is what the
        # alert actually fires on and is what makes the failure visible to the diagnosis.
        context["unavailable_replicas"] = query_prometheus(
            incident_id,
            f'kube_deployment_status_replicas_unavailable{{namespace="{ns}", deployment="{name}"}}',
        )

    return context
