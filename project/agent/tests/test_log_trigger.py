"""Unit tests for the log-driven trigger (Phase A) - classification, Loki
response parsing, and error-signature dedup. No live Loki/cluster needed."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("KUBE_IN_CLUSTER", "false")

from app import log_watcher
from app.config import settings
from app.diagnosis import log_classifier
from app.diagnosis.log_classifier import classify_log_event, classify_log_line


class LogClassifierTests(unittest.TestCase):
    def test_crashloop_keyword_maps_to_crashloop(self):
        c = classify_log_line("demo", "demo-web-abc123", "Back-off restarting failed container")
        self.assertEqual(c.incident_type, "crashloop")
        self.assertEqual(c.resource_name, "demo-web-abc123")

    def test_connectivity_keyword_maps_to_networkpolicy_block(self):
        c = classify_log_line("demo", "demo-web-abc123", "dial tcp: connection refused")
        self.assertEqual(c.incident_type, "networkpolicy_block")

    def test_deployment_shaped_type_strips_pod_hash(self):
        c = classify_log_line("demo", "demo-web-7d9f6c5b8c-x2k4p", "ImagePullBackOff pulling image")
        self.assertEqual(c.incident_type, "replica_mismatch")
        self.assertEqual(c.resource_name, "demo-web")

    def test_unrecognised_line_is_unknown(self):
        c = classify_log_line("demo", "demo-web-abc123", "just a normal error happened")
        # "error" matches the watcher regex but not a specific incident_type rule
        self.assertEqual(c.incident_type, "unknown")


class LogWatcherParsingTests(unittest.TestCase):
    def test_extract_events_flattens_streams(self):
        response = {
            "data": {
                "result": [
                    {
                        "stream": {"namespace": "demo", "pod": "demo-web-1"},
                        "values": [["170000", "panic: boom"], ["170001", "error: retry"]],
                    },
                    {"stream": {"namespace": "demo", "pod": "demo-web-2"}, "values": [["170002", "oomkilled"]]},
                ]
            }
        }
        events = log_watcher._extract_events(response)
        self.assertEqual(len(events), 3)
        self.assertIn(("demo", "demo-web-1", "panic: boom"), events)

    def test_extract_events_tolerates_malformed_response(self):
        self.assertEqual(log_watcher._extract_events({}), [])
        self.assertEqual(log_watcher._extract_events({"data": {}}), [])

    def test_signature_collapses_ips_and_numbers(self):
        s1 = log_watcher._signature("connection refused to 10.1.2.3:8080")
        s2 = log_watcher._signature("connection refused to 10.9.9.9:8081")
        self.assertEqual(s1, s2)


class AgenticLogClassifierTests(unittest.TestCase):
    def setUp(self):
        self._prev_flag = settings.agentic_log_classification
        self._prev_bedrock = settings.bedrock_enabled
        self._prev_conf = settings.log_classification_min_confidence
        settings.agentic_log_classification = True
        settings.bedrock_enabled = True
        settings.log_classification_min_confidence = 0.5

    def tearDown(self):
        settings.agentic_log_classification = self._prev_flag
        settings.bedrock_enabled = self._prev_bedrock
        settings.log_classification_min_confidence = self._prev_conf

    def test_llm_upgrades_unknown_line_when_confident(self):
        with patch("app.graph.llm.analyze", return_value='{"incident_type": "crashloop", "confidence": 0.9}'):
            c = classify_log_event("demo", "demo-web-abc123", "weird error nobody keyworded")
        self.assertEqual(c.incident_type, "crashloop")
        self.assertEqual(c.annotations.get("classified_by"), "llm")

    def test_llm_low_confidence_stays_unknown(self):
        with patch("app.graph.llm.analyze", return_value='{"incident_type": "crashloop", "confidence": 0.2}'):
            c = classify_log_event("demo", "demo-web-abc123", "weird error nobody keyworded")
        self.assertEqual(c.incident_type, "unknown")

    def test_llm_invalid_type_stays_unknown(self):
        with patch("app.graph.llm.analyze", return_value='{"incident_type": "made_up", "confidence": 0.99}'):
            c = classify_log_event("demo", "demo-web-abc123", "weird error nobody keyworded")
        self.assertEqual(c.incident_type, "unknown")

    def test_keyword_hit_skips_the_llm(self):
        with patch("app.graph.llm.analyze") as mock_analyze:
            c = classify_log_event("demo", "demo-web-abc123", "Back-off restarting failed container")
        self.assertEqual(c.incident_type, "crashloop")
        mock_analyze.assert_not_called()

    def test_disabled_flag_skips_the_llm(self):
        settings.agentic_log_classification = False
        with patch("app.graph.llm.analyze") as mock_analyze:
            c = classify_log_event("demo", "demo-web-abc123", "weird error nobody keyworded")
        self.assertEqual(c.incident_type, "unknown")
        mock_analyze.assert_not_called()


if __name__ == "__main__":
    unittest.main()
