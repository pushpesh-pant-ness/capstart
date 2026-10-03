"""Tests for the historical retrieval node's hybrid (pgvector) path and its
fallback to the keyword path. Bedrock embedding and all DB access are mocked -
no Bedrock or Postgres needed."""
from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("KUBE_IN_CLUSTER", "false")

from app.config import settings
from app.graph.nodes import historical


def _row(id_, resource, severity, vector_similarity):
    return {
        "id": id_,
        "resource_name": resource,
        "computed_severity": severity,
        "status": "resolved",
        "remediation_plan": json.dumps({"action": "restart_pod"}),
        "vector_similarity": vector_similarity,
    }


class HybridHistoricalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._prev = (settings.hybrid_retrieval_enabled, settings.bedrock_enabled, settings.hybrid_vector_weight)
        settings.hybrid_retrieval_enabled = True
        settings.bedrock_enabled = True
        settings.hybrid_vector_weight = 0.5

    def tearDown(self):
        settings.hybrid_retrieval_enabled, settings.bedrock_enabled, settings.hybrid_vector_weight = self._prev

    async def test_heuristic_gate_outranks_higher_vector_similarity(self):
        state = {"incident_id": 1, "incident_type": "crashloop", "resource_name": "web", "severity": "P4"}
        rows = [
            _row(11, "other", "P2", vector_similarity=0.99),  # closest vector, wrong resource
            _row(10, "web", "P4", vector_similarity=0.90),     # weaker vector, exact resource+severity
        ]
        with patch("app.graph.nodes.historical.bedrock_client.embed_text", return_value=[0.1] * 4), \
             patch("app.graph.nodes.historical.audit.set_incident_embedding") as set_emb, \
             patch("app.graph.nodes.historical.audit.find_similar_resolved_hybrid", return_value=rows):
            update = await historical.historical(state)

        candidates = update["similar_incidents"]
        set_emb.assert_called_once()  # current incident embedding persisted for future search
        self.assertEqual(candidates[0]["incident_id"], 10)  # heuristic gate wins
        self.assertAlmostEqual(candidates[0]["similarity_score"], 0.95, places=3)  # 0.5*0.90 + 0.5*1.0
        self.assertAlmostEqual(candidates[1]["similarity_score"], 0.495, places=3)  # 0.5*0.99 + 0.5*0.0
        self.assertEqual(candidates[0]["remediation_plan"], {"action": "restart_pod"})

    async def test_falls_back_to_keyword_when_embedding_unavailable(self):
        state = {"incident_id": 1, "incident_type": "crashloop", "resource_name": "web", "severity": "P4"}
        with patch("app.graph.nodes.historical.bedrock_client.embed_text", return_value=None), \
             patch("app.graph.nodes.historical.audit.find_similar_resolved_hybrid") as hybrid, \
             patch("app.graph.nodes.historical.audit.find_similar_resolved", return_value=[]) as keyword:
            update = await historical.historical(state)

        hybrid.assert_not_called()
        keyword.assert_called_once()
        self.assertEqual(update["similar_incidents"], [])

    async def test_keyword_path_when_flag_disabled(self):
        settings.hybrid_retrieval_enabled = False
        state = {"incident_id": 1, "incident_type": "crashloop", "resource_name": "web", "severity": "P4"}
        with patch("app.graph.nodes.historical.bedrock_client.embed_text") as embed, \
             patch("app.graph.nodes.historical.audit.find_similar_resolved", return_value=[]) as keyword:
            await historical.historical(state)

        embed.assert_not_called()
        keyword.assert_called_once()


if __name__ == "__main__":
    unittest.main()
