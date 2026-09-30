"""Unit tests for the diagnosis/planning graph - run with:
    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
No pytest dependency needed (stdlib unittest + unittest.mock only), and no
real AWS credentials or a live cluster are required - every Bedrock/K8s call
is mocked so these exercise the actual parsing/routing/guardrail logic that
previously only ran end-to-end against fallbacks.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("AUDIT_DB_PATH", os.path.join(os.path.dirname(__file__), "_test_audit.db"))
os.environ.setdefault("KUBE_IN_CLUSTER", "false")

from app import bedrock_client
from app.config import settings
from app.graph import guardrail
from app.graph.llm import LLMUnavailableError
from app.graph.nodes.guardrail import route_after_guardrail
from app.graph.nodes.plan import plan
from app.graph.nodes.rca import route_after_rca
from app.graph.nodes.supervisor import find_auto_plan_candidate


class SplitConfidenceTests(unittest.TestCase):
    def test_extracts_and_strips_trailing_confidence_line(self):
        text = "The pod is crash-looping due to a bad image.\nCONFIDENCE: 0.85"
        remaining, confidence = bedrock_client._split_confidence(text)
        self.assertEqual(remaining, "The pod is crash-looping due to a bad image.")
        self.assertAlmostEqual(confidence, 0.85)

    def test_missing_confidence_line_uses_default(self):
        remaining, confidence = bedrock_client._split_confidence("No confidence line here.")
        self.assertEqual(remaining, "No confidence line here.")
        self.assertEqual(confidence, 0.5)

    def test_out_of_range_confidence_is_clamped(self):
        _, confidence = bedrock_client._split_confidence("text\nCONFIDENCE: 1.7")
        self.assertEqual(confidence, 1.0)


class GuardrailTests(unittest.TestCase):
    def test_rejects_action_not_in_allowlist_for_type(self):
        plan_body = {"title": "t", "steps": ["s"], "action": "recover_node", "target": {"namespace": "demo", "name": "x"}}
        reason = guardrail.check_plan(plan_body, incident_type="crashloop", expected_namespace="demo", expected_resource_name="x")
        self.assertIsNotNone(reason)
        self.assertIn("delete_pod", reason)

    def test_rejects_target_mismatch(self):
        plan_body = {"title": "t", "steps": ["s"], "action": "delete_pod", "target": {"namespace": "demo", "name": "other"}}
        reason = guardrail.check_plan(plan_body, incident_type="crashloop", expected_namespace="demo", expected_resource_name="x")
        self.assertIsNotNone(reason)

    def test_accepts_valid_plan(self):
        plan_body = {"title": "t", "steps": ["s"], "action": "delete_pod", "target": {"namespace": "demo", "name": "x"}}
        reason = guardrail.check_plan(plan_body, incident_type="crashloop", expected_namespace="demo", expected_resource_name="x")
        self.assertIsNone(reason)

    def test_route_after_guardrail(self):
        self.assertEqual(route_after_guardrail({"escalation_reason": "bad"}), "escalate")
        self.assertNotEqual(route_after_guardrail({}), "escalate")


class RoutingTests(unittest.TestCase):
    def test_route_after_rca_escalates_on_low_confidence(self):
        self.assertEqual(route_after_rca({"low_confidence": True}), "escalate")
        self.assertEqual(route_after_rca({"low_confidence": False}), "plan")

    def test_auto_plan_candidate_requires_threshold_and_plan(self):
        state = {"severity": "P4", "similar_incidents": [{"similarity_score": 0.9, "remediation_plan": {"action": "delete_pod"}}]}
        self.assertIsNotNone(find_auto_plan_candidate(state))

        below_threshold = {"severity": "P4", "similar_incidents": [{"similarity_score": 0.5, "remediation_plan": {"action": "delete_pod"}}]}
        self.assertIsNone(find_auto_plan_candidate(below_threshold))

        wrong_severity = {"severity": "P2", "similar_incidents": [{"similarity_score": 0.9, "remediation_plan": {"action": "delete_pod"}}]}
        self.assertIsNone(find_auto_plan_candidate(wrong_severity))


class PlanNodeTests(unittest.IsolatedAsyncioTestCase):
    base_state = {
        "incident_id": 1,
        "incident_type": "crashloop",
        "namespace": "demo",
        "resource_name": "demo-web-abc",
        "context": {},
        "diagnosis_text": "Pod is crash-looping.",
        "confidence_score": 0.8,
        "severity": "P2",
        "severity_rationale": "elevated restarts",
    }

    async def test_default_mode_keeps_narrative_but_ignores_llm_action(self):
        """settings.llm_authors_action=False (default): even if the model
        names the wrong action, the narrative survives and the action is
        still forced to the deterministic one - never silently discarded."""
        llm_json = '{"action": "recover_node", "title": "Custom title", "steps": ["step one", "step two"]}'
        with patch.object(settings, "llm_authors_action", False), patch("app.graph.nodes.plan.llm.analyze", return_value=llm_json):
            result = await plan(dict(self.base_state))
        remediation_plan = result["remediation_plan"]
        self.assertEqual(remediation_plan["action"], "delete_pod")
        self.assertEqual(remediation_plan["title"], "Custom title")
        self.assertEqual(remediation_plan["target"], {"namespace": "demo", "name": "demo-web-abc"})

    async def test_llm_authors_action_mode_accepts_matching_action(self):
        llm_json = '{"action": "delete_pod", "title": "Custom title", "steps": ["step one"]}'
        with patch.object(settings, "llm_authors_action", True), patch("app.graph.nodes.plan.llm.analyze", return_value=llm_json):
            result = await plan(dict(self.base_state))
        self.assertEqual(result["remediation_plan"]["action"], "delete_pod")
        self.assertEqual(result["remediation_plan"]["title"], "Custom title")

    async def test_llm_authors_action_mode_rejects_mismatched_action(self):
        llm_json = '{"action": "recover_node", "title": "Custom title", "steps": ["step one"]}'
        with patch.object(settings, "llm_authors_action", True), patch("app.graph.nodes.plan.llm.analyze", return_value=llm_json):
            result = await plan(dict(self.base_state))
        remediation_plan = result["remediation_plan"]
        # Falls back to the deterministic template entirely - never a
        # mismatched action paired with an LLM-authored narrative.
        self.assertEqual(remediation_plan["action"], "delete_pod")
        self.assertNotEqual(remediation_plan["title"], "Custom title")

    async def test_falls_back_to_deterministic_plan_when_llm_unavailable(self):
        with patch("app.graph.nodes.plan.llm.analyze", side_effect=LLMUnavailableError("boom")):
            result = await plan(dict(self.base_state))
        self.assertEqual(result["remediation_plan"]["action"], "delete_pod")


if __name__ == "__main__":
    unittest.main()
