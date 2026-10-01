"""Phase 1 tests for the agentic investigate node: it merges the agent's
summary + evidence trail when enabled, and falls back to deterministic context
when the agent is unavailable. The react agent and Prometheus/Loki calls are
mocked - no live cluster or Bedrock needed."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("KUBE_IN_CLUSTER", "false")

from app.graph import react_investigator
from app.graph.nodes import investigate as investigate_node

_STATE = {
    "incident_id": 1,
    "incident_type": "crashloop",
    "namespace": "demo",
    "resource_name": "demo-web-abc",
    "alert_severity": "warning",
    "labels": {},
    "annotations": {},
    "raw_alert": {"labels": {"alertname": "PodCrashLooping"}},
}


class InvestigateNodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_fallback_to_deterministic_context_when_agent_unavailable(self):
        async def _raise(**_):
            raise react_investigator.InvestigationUnavailable("disabled")

        with patch.object(investigate_node, "build_context", return_value={"incident_type": "crashloop"}), \
             patch.object(investigate_node.react_investigator, "investigate_agentically", side_effect=_raise):
            result = await investigate_node.investigate(dict(_STATE))

        self.assertEqual(result["context"], {"incident_type": "crashloop"})
        self.assertEqual(result["evidence_trail"], [])
        self.assertNotIn("agent_investigation", result["context"])

    async def test_agentic_investigation_merges_summary_and_trail(self):
        async def _agent(**_):
            return {
                "summary": "Pod is crash-looping due to a bad command.",
                "evidence_trail": [{"tool": "get_pod", "args": {"name": "demo-web-abc"}}],
            }

        with patch.object(investigate_node, "build_context", return_value={"incident_type": "crashloop"}), \
             patch.object(investigate_node.react_investigator, "investigate_agentically", side_effect=_agent):
            result = await investigate_node.investigate(dict(_STATE))

        self.assertEqual(result["context"]["agent_investigation"], "Pod is crash-looping due to a bad command.")
        self.assertEqual(len(result["evidence_trail"]), 1)
        self.assertEqual(result["evidence_trail"][0]["tool"], "get_pod")


if __name__ == "__main__":
    unittest.main()
