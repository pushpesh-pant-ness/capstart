"""Phase 3 tests for the reflection/self-critique node: ok proceeds to the
guardrail, insufficient loops back to investigate (until the hop budget caps
it). llm.analyze is mocked - no Bedrock needed."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("KUBE_IN_CLUSTER", "false")

from app.config import settings
from app.graph.nodes import reflect as rfl


def _reflective(fn):
    async def wrapper(*args, **kwargs):
        prev_a, prev_b = settings.agentic_reflection, settings.bedrock_enabled
        settings.agentic_reflection, settings.bedrock_enabled = True, True
        try:
            return await fn(*args, **kwargs)
        finally:
            settings.agentic_reflection, settings.bedrock_enabled = prev_a, prev_b
    return wrapper


class ReflectNodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_is_noop_and_routes_to_guardrail(self):
        with patch.object(settings, "agentic_reflection", False):
            self.assertIsNone(await rfl.reflect({"incident_id": 1}))
            self.assertEqual(rfl.route_after_reflect({}), "guardrail")

    @_reflective
    async def test_ok_verdict_routes_to_guardrail(self):
        with patch("app.graph.llm.analyze", return_value='{"verdict": "ok", "rationale": "plan fits"}'):
            update = await rfl.reflect({"incident_id": 1, "supervisor_hops": 0})
        self.assertEqual(rfl.route_after_reflect(update), "guardrail")
        self.assertTrue(update["reflections"][0].startswith("ok:"))

    @_reflective
    async def test_insufficient_under_budget_loops_to_investigate(self):
        with patch("app.graph.llm.analyze", return_value='{"verdict": "insufficient", "rationale": "thin"}'):
            update = await rfl.reflect({"incident_id": 1, "supervisor_hops": 0})
        self.assertEqual(rfl.route_after_reflect(update), "investigate")
        self.assertEqual(update["supervisor_hops"], 1)

    @_reflective
    async def test_insufficient_at_budget_cap_proceeds_to_guardrail(self):
        state = {"incident_id": 1, "supervisor_hops": settings.agent_max_steps}
        with patch("app.graph.llm.analyze", return_value='{"verdict": "insufficient", "rationale": "thin"}'):
            update = await rfl.reflect(state)
        self.assertEqual(rfl.route_after_reflect(update), "guardrail")


if __name__ == "__main__":
    unittest.main()
