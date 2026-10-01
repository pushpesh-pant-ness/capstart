"""Phase 2 tests for the agentic supervisor/router: LLM decisions, the
deterministic backstops (auto_plan needs a real candidate, gather_more is
hop-capped), and route mapping. llm.analyze is mocked - no Bedrock needed."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("KUBE_IN_CLUSTER", "false")

from app.config import settings
from app.graph.nodes import supervisor as sup


def _agentic(fn):
    async def wrapper(*args, **kwargs):
        prev_a, prev_b = settings.agentic_supervisor, settings.bedrock_enabled
        settings.agentic_supervisor, settings.bedrock_enabled = True, True
        try:
            return await fn(*args, **kwargs)
        finally:
            settings.agentic_supervisor, settings.bedrock_enabled = prev_a, prev_b
    return wrapper


class SupervisorRoutingTests(unittest.IsolatedAsyncioTestCase):
    @_agentic
    async def test_auto_plan_without_candidate_is_backstopped_to_rca(self):
        state = {"incident_id": 1, "severity": "P2", "similar_incidents": []}
        with patch("app.graph.llm.analyze", return_value='{"decision": "auto_plan", "rationale": "looks familiar"}'):
            update = await sup.supervisor(state)
        self.assertEqual(update["router_decision"], "full_rca")
        self.assertIn("backstop", update["router_rationale"])

    @_agentic
    async def test_gather_more_hop_cap_forces_rca(self):
        state = {"incident_id": 1, "severity": "P2", "supervisor_hops": settings.agent_max_steps}
        with patch("app.graph.llm.analyze", return_value='{"decision": "gather_more", "rationale": "thin"}'):
            update = await sup.supervisor(state)
        self.assertEqual(update["router_decision"], "full_rca")

    @_agentic
    async def test_gather_more_under_cap_increments_hops(self):
        state = {"incident_id": 1, "severity": "P2", "supervisor_hops": 0}
        with patch("app.graph.llm.analyze", return_value='{"decision": "gather_more", "rationale": "need logs"}'):
            update = await sup.supervisor(state)
        self.assertEqual(update["router_decision"], "gather_more")
        self.assertEqual(update["supervisor_hops"], 1)

    @_agentic
    async def test_escalate_sets_escalation_reason(self):
        state = {"incident_id": 1, "severity": "P1"}
        with patch("app.graph.llm.analyze", return_value='{"decision": "escalate", "rationale": "too risky"}'):
            update = await sup.supervisor(state)
        self.assertEqual(update["router_decision"], "escalate")
        self.assertIn("too risky", update["escalation_reason"])

    @_agentic
    async def test_unknown_decision_defaults_to_rca(self):
        state = {"incident_id": 1, "severity": "P2"}
        with patch("app.graph.llm.analyze", return_value='{"decision": "nuke_it", "rationale": "why not"}'):
            update = await sup.supervisor(state)
        self.assertEqual(update["router_decision"], "full_rca")

    @_agentic
    async def test_route_after_supervisor_maps_decisions(self):
        self.assertEqual(sup.route_after_supervisor({"router_decision": "gather_more"}), "investigate")
        self.assertEqual(sup.route_after_supervisor({"router_decision": "auto_plan"}), "auto_plan")
        self.assertEqual(sup.route_after_supervisor({"router_decision": "full_rca"}), "rca")
        self.assertEqual(sup.route_after_supervisor({"router_decision": "escalate"}), "escalate")

    async def test_deterministic_mode_supervisor_is_noop(self):
        # agentic flags off (default): node returns None, routing by the old rule.
        self.assertFalse(settings.agentic_supervisor)
        self.assertIsNone(await sup.supervisor({"incident_id": 1, "severity": "P2"}))
        self.assertEqual(sup.route_after_supervisor({"severity": "P2", "similar_incidents": []}), "rca")


if __name__ == "__main__":
    unittest.main()
