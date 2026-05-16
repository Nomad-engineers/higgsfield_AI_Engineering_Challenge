"""Hermetic tests for RRF merge and formatting functions."""
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest

from src.services.recall_service import (
    rrf_merge,
    format_stable_facts,
    format_relevant_memories,
    estimate_tokens,
)


def _make_memory(key="test_key", value="test_value", days_old=0, confidence=1.0):
    m = MagicMock()
    m.id = uuid.uuid4()
    m.key = key
    m.value = value
    m.type = "fact"
    m.confidence = confidence
    m.created_at = datetime.now(timezone.utc) - timedelta(days=days_old)
    m.source_turn_id = uuid.uuid4()
    m.source_session = "test-session"
    return m


class TestRRFMerge:
    def test_empty_inputs(self):
        result = rrf_merge([], [])
        assert result == []

    def test_vector_only(self):
        m = _make_memory()
        result = rrf_merge([(m, 0.9)], [])
        assert len(result) == 1
        assert result[0][0].id == m.id

    def test_bm25_only(self):
        m = _make_memory()
        result = rrf_merge([], [(m, 0.8)])
        assert len(result) == 1
        assert result[0][0].id == m.id

    def test_dedup_across_sources(self):
        m = _make_memory()
        result = rrf_merge([(m, 0.9)], [(m, 0.8)])
        assert len(result) == 1
        # Should have higher score than either single source
        assert result[0][1] > 1.0 / (60 + 0 + 1)

    def test_key_results_boost(self):
        m1 = _make_memory(key="employer", value="Company A")
        m2 = _make_memory(key="pet", value="Dog")

        result_no_key = rrf_merge([(m1, 0.9)], [(m2, 0.8)])
        result_with_key = rrf_merge([(m1, 0.9)], [(m2, 0.8)], key_results=[(m1, 1.0)])

        # m1 should score higher with key boost
        score_no = next(s for mem, s in result_no_key if mem.id == m1.id)
        score_yes = next(s for mem, s in result_with_key if mem.id == m1.id)
        assert score_yes > score_no

    def test_recency_boost(self):
        recent = _make_memory(days_old=0)
        old = _make_memory(days_old=365)

        result = rrf_merge([(recent, 0.5), (old, 0.5)], [])
        recent_score = next(s for m, s in result if m.id == recent.id)
        old_score = next(s for m, s in result if m.id == old.id)
        assert recent_score > old_score

    def test_same_session_boost(self):
        m1 = _make_memory(key="location", value="Berlin")
        m1.source_session = "active-session"
        m2 = _make_memory(key="employer", value="Stripe")
        m2.source_session = "other-session"

        result_no_session = rrf_merge([(m1, 0.5), (m2, 0.5)], [])
        result_with_session = rrf_merge([(m1, 0.5), (m2, 0.5)], [], session_id="active-session")

        score_no = next(s for m, s in result_no_session if m.id == m1.id)
        score_yes = next(s for m, s in result_with_session if m.id == m1.id)
        assert score_yes > score_no

    def test_no_session_boost_without_session_id(self):
        m = _make_memory(key="location", value="Berlin")
        m.source_session = "active-session"

        result = rrf_merge([(m, 0.5)], [])
        assert len(result) == 1

    def test_ordering_by_score(self):
        m1 = _make_memory(key="a")
        m2 = _make_memory(key="b")
        m3 = _make_memory(key="c")

        # m1 appears in both vector and bm25
        result = rrf_merge([(m1, 0.9), (m2, 0.8)], [(m1, 0.9), (m3, 0.7)])
        ids = [m.id for m, _ in result]
        assert ids[0] == m1.id


class TestFormatStableFacts:
    def test_empty(self):
        assert format_stable_facts([], 512) == ""

    def test_single_fact(self):
        m = _make_memory(key="employer", value="Stripe")
        result = format_stable_facts([m], 512)
        assert "employer" in result
        assert "Stripe" in result

    def test_evolution_shows_previous(self):
        old = _make_memory(key="employer", value="Notion", days_old=30)
        new = _make_memory(key="employer", value="Stripe", days_old=0)
        result = format_stable_facts([new, old], 512)
        assert "Stripe" in result
        assert "Notion" in result

    def test_budget_respected(self):
        m = _make_memory(key="k", value="v" * 1000)
        result = format_stable_facts([m], 10)
        assert len(result) < 500


class TestFormatRelevantMemories:
    def test_empty(self):
        text, cites = format_relevant_memories([], 512)
        assert text == ""
        assert cites == []

    def test_single_memory(self):
        m = _make_memory(key="pet", value="Biscuit the golden retriever")
        text, cites = format_relevant_memories([(m, 0.85)], 512)
        assert "Biscuit" in text
        assert len(cites) == 1
        assert cites[0]["score"] == 0.85

    def test_dedup_same_key_shows_newest(self):
        old = _make_memory(key="employer", value="Notion", days_old=30)
        new = _make_memory(key="employer", value="Stripe", days_old=0)
        text, cites = format_relevant_memories([(new, 0.9), (old, 0.7)], 512)
        assert "Stripe" in text
        assert len(cites) == 2


class TestEstimateTokens:
    def test_empty(self):
        assert estimate_tokens("") == 1

    def test_short_text(self):
        assert estimate_tokens("hello") == 1  # max(1, len//3) = max(1, 1)

    def test_long_text(self):
        text = "a" * 300
        assert estimate_tokens(text) == 100
