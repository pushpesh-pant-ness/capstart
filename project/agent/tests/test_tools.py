"""Phase 0 smoke tests for the read-only diagnostic toolbelt. No live cluster
needed - tools that reach the API just return an {"error": ...} dict."""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("KUBE_IN_CLUSTER", "false")

from app.graph import tools


class ToolbeltTests(unittest.TestCase):
    def test_expected_tools_registered_and_callable(self):
        expected = {
            "prometheus_query", "loki_logs", "get_pod", "describe_deployment",
            "list_events", "get_node", "get_endpoints", "list_networkpolicies",
        }
        self.assertEqual(set(tools.READ_ONLY_TOOLS), expected)
        for fn in tools.READ_ONLY_TOOLS.values():
            self.assertTrue(callable(fn))

    def test_kube_tools_return_error_dict_without_cluster(self):
        # No mutation, never raises: an unreachable API surfaces as an error dict.
        result = tools.get_node("nonexistent-node")
        self.assertIsInstance(result, dict)


if __name__ == "__main__":
    unittest.main()
