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
from unittest.mock import Mock, patch

os.environ.setdefault("KUBE_IN_CLUSTER", "false")

from app import bedrock_client
from app.config import settings
from app.graph import guardrail
from app.graph import llm as graph_llm
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

    def test_accepts_second_allow_listed_action_for_type(self):
        # crashloop now allows delete_pod OR rollout_restart_deployment.
        plan_body = {"title": "t", "steps": ["s"], "action": "rollout_restart_deployment", "target": {"namespace": "demo", "name": "x"}}
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
        # Primary path: the LLM selects the tool via a real tool call.
        with patch.object(settings, "llm_authors_action", True), patch(
            "app.graph.nodes.plan.llm.select_tool",
            return_value=("delete_pod", {"title": "Custom title", "steps": ["step one"]}),
        ):
            result = await plan(dict(self.base_state))
        self.assertEqual(result["remediation_plan"]["action"], "delete_pod")
        self.assertEqual(result["remediation_plan"]["title"], "Custom title")
        self.assertEqual(result["remediation_plan"]["selected_via"], "tool_call")

    async def test_llm_authors_action_mode_accepts_alternate_allow_listed_tool(self):
        # The model may call any tool on the incident type's allow-list, not just
        # the default - here the second crashloop candidate.
        with patch.object(settings, "llm_authors_action", True), patch(
            "app.graph.nodes.plan.llm.select_tool",
            return_value=("rollout_restart_deployment", {"title": "Restart rollout", "steps": ["step one"]}),
        ):
            result = await plan(dict(self.base_state))
        self.assertEqual(result["remediation_plan"]["action"], "rollout_restart_deployment")
        self.assertEqual(result["remediation_plan"]["title"], "Restart rollout")
        self.assertEqual(result["remediation_plan"]["selected_via"], "tool_call")

    async def test_llm_authors_action_mode_rejects_mismatched_tool_call(self):
        # The model calls a tool that isn't allow-listed for this type -> the
        # tool-call path is discarded and we fall back; with the text path also
        # unavailable, it lands on the deterministic template.
        with patch.object(settings, "llm_authors_action", True), patch(
            "app.graph.nodes.plan.llm.select_tool",
            return_value=("recover_node", {"title": "Custom title", "steps": ["step one"]}),
        ), patch("app.graph.nodes.plan.llm.analyze", side_effect=LLMUnavailableError("no text fallback")):
            result = await plan(dict(self.base_state))
        remediation_plan = result["remediation_plan"]
        self.assertEqual(remediation_plan["action"], "delete_pod")
        self.assertNotEqual(remediation_plan["title"], "Custom title")

    async def test_falls_back_to_deterministic_plan_when_llm_unavailable(self):
        with patch.object(settings, "llm_authors_action", True), patch(
            "app.graph.nodes.plan.llm.select_tool", side_effect=LLMUnavailableError("boom")
        ), patch("app.graph.nodes.plan.llm.analyze", side_effect=LLMUnavailableError("boom")):
            result = await plan(dict(self.base_state))
        self.assertEqual(result["remediation_plan"]["action"], "delete_pod")
        self.assertEqual(result["remediation_plan"]["selected_via"], "default")


class SelectToolTests(unittest.TestCase):
    def _fake_client(self, response):
        client = Mock()
        client.converse.return_value = response
        return client

    def test_returns_tool_name_and_input_from_tool_use_block(self):
        response = {
            "output": {"message": {"content": [
                {"text": "I'll fix this."},
                {"toolUse": {"toolUseId": "t1", "name": "delete_pod", "input": {"title": "T", "steps": ["s"]}}},
            ]}}
        }
        with patch.object(settings, "bedrock_enabled", True), patch(
            "app.graph.llm._get_client", return_value=self._fake_client(response)
        ):
            name, tool_input = graph_llm.select_tool(1, "plan.tool_call", "sys", "user", [{"toolSpec": {"name": "delete_pod"}}])
        self.assertEqual(name, "delete_pod")
        self.assertEqual(tool_input, {"title": "T", "steps": ["s"]})

    def test_raises_when_model_answers_in_text_only(self):
        response = {"output": {"message": {"content": [{"text": "no tool call here"}]}}}
        with patch.object(settings, "bedrock_enabled", True), patch(
            "app.graph.llm._get_client", return_value=self._fake_client(response)
        ):
            with self.assertRaises(LLMUnavailableError):
                graph_llm.select_tool(1, "plan.tool_call", "sys", "user", [{"toolSpec": {"name": "delete_pod"}}])


if __name__ == "__main__":
    unittest.main()
