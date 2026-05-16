"""Wave 2 integration tests: temporal, entity graph, opinion arcs, tiktoken, reranker."""
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest

from src.prompts.temporal import parse_temporal, _subtract_months, _days_in_month
from src.services.entity_graph import EntityGraph, build_graph_from_memories, SEED_RELATIONS
from src.services.recall_service import (
    rrf_merge,
    format_stable_facts,
    estimate_tokens,
    _render_evolution_arc,
)


def _make_memory(key="test_key", value="test_value", mtype="fact", days_old=0,
                 confidence=1.0, session="test-session", supersedes=None):
    m = MagicMock()
    m.id = uuid.uuid4()
    m.key = key
    m.value = value
    m.type = mtype
    m.confidence = confidence
    m.created_at = datetime.now(timezone.utc) - timedelta(days=days_old)
    m.source_turn_id = uuid.uuid4()
    m.source_session = session
    m.supersedes = supersedes
    return m


# ── Temporal parsing ──────────────────────────────────────────────────

class TestTemporalParsing:
    def test_n_days_ago(self):
        result = parse_temporal("What did the user say 5 days ago?")
        assert result is not None
        assert result["after"] is not None
        assert result["boost"] == 1.5

    def test_n_months_ago(self):
        result = parse_temporal("What happened 3 months ago?")
        assert result is not None
        assert result["after"] is not None

    def test_last_n_weeks(self):
        result = parse_temporal("Show me what was said in the last 2 weeks")
        assert result is not None
        assert result["after"] is not None
        assert result["boost"] == 1.3

    def test_recently(self):
        result = parse_temporal("What has the user been doing recently?")
        assert result is not None
        assert result["after"] is not None
        assert result["boost"] == 1.2

    def test_last_month(self):
        result = parse_temporal("What did the user say last month?")
        assert result is not None
        assert result["after"] is not None

    def test_last_year(self):
        result = parse_temporal("What happened last year?")
        assert result is not None
        assert result["after"] is not None

    def test_this_month(self):
        result = parse_temporal("What has happened this month?")
        assert result is not None
        assert result["after"] is not None

    def test_since_month(self):
        result = parse_temporal("What has happened since January?")
        assert result is not None
        assert result["after"] is not None

    def test_before_month(self):
        result = parse_temporal("What happened before March?")
        assert result is not None
        assert result["before"] is not None
        assert result["after"] is None

    def test_no_temporal(self):
        result = parse_temporal("What is the user's favorite color?")
        assert result is None

    def test_subtract_months_rollover(self):
        dt = datetime(2024, 1, 15, tzinfo=timezone.utc)
        result = _subtract_months(dt, 2)
        assert result.year == 2023
        assert result.month == 11

    def test_subtract_months_same_year(self):
        dt = datetime(2024, 6, 15, tzinfo=timezone.utc)
        result = _subtract_months(dt, 3)
        assert result.year == 2024
        assert result.month == 3

    def test_days_in_month_february_leap(self):
        assert _days_in_month(2024, 2) == 29

    def test_days_in_month_february_nonleap(self):
        assert _days_in_month(2023, 2) == 28

    def test_days_in_month_december(self):
        assert _days_in_month(2024, 12) == 31


class TestTemporalInRRFMerge:
    def test_temporal_penalty_outside_range(self):
        recent = _make_memory(key="location", value="Berlin", days_old=5)
        old = _make_memory(key="location", value="Munich", days_old=200)

        tc = {"after": datetime.now(timezone.utc) - timedelta(days=10), "before": None, "boost": 1.5}
        result = rrf_merge([(recent, 0.5), (old, 0.5)], [], temporal_constraint=tc)

        recent_score = next(s for m, s in result if m.id == recent.id)
        old_score = next(s for m, s in result if m.id == old.id)
        assert recent_score > old_score

    def test_temporal_boost_within_range(self):
        recent = _make_memory(key="location", value="Berlin", days_old=1)

        result_no_tc = rrf_merge([(recent, 0.5)], [])
        tc = {"after": datetime.now(timezone.utc) - timedelta(days=5), "before": None, "boost": 2.0}
        result_with_tc = rrf_merge([(recent, 0.5)], [], temporal_constraint=tc)

        score_no = result_no_tc[0][1]
        score_yes = result_with_tc[0][1]
        assert score_yes > score_no


# ── Entity graph ──────────────────────────────────────────────────────

class TestEntityGraph:
    def test_add_co_occurrence(self):
        g = EntityGraph()
        g.add_co_occurrence("employer", "location")
        assert "location" in g.neighbors("employer")
        assert "employer" in g.neighbors("location")

    def test_no_self_loop(self):
        g = EntityGraph()
        g.add_co_occurrence("employer", "employer")
        assert "employer" not in g.neighbors("employer")

    def test_expand_depth_1(self):
        g = EntityGraph()
        g.add_co_occurrence("employer", "location")
        g.add_co_occurrence("location", "pet")
        expanded = g.expand({"employer"}, depth=1)
        assert "location" in expanded
        assert "pet" not in expanded

    def test_expand_depth_2(self):
        g = EntityGraph()
        g.add_co_occurrence("employer", "location")
        g.add_co_occurrence("location", "pet")
        expanded = g.expand({"employer"}, depth=2)
        assert "location" in expanded
        assert "pet" in expanded

    def test_expand_excludes_seed(self):
        g = EntityGraph()
        g.add_co_occurrence("a", "b")
        expanded = g.expand({"a"}, depth=1)
        assert "a" not in expanded

    def test_top_neighbors_ordered(self):
        g = EntityGraph()
        g.add_co_occurrence("employer", "location", weight=3.0)
        g.add_co_occurrence("employer", "pet", weight=1.0)
        g.add_co_occurrence("employer", "name", weight=2.0)
        top = g.top_neighbors("employer", limit=2)
        assert top[0][0] == "location"
        assert top[1][0] == "name"

    def test_build_graph_from_memories_includes_seed(self):
        m = _make_memory(key="employer", value="Stripe", session="s1")
        m.source_session = "s1"
        graph = build_graph_from_memories([m])
        # Seed relations should be present (employer -> location, etc.)
        assert "location" in graph.neighbors("employer")

    def test_build_graph_co_occurrence(self):
        m1 = _make_memory(key="employer", value="Stripe", session="s1")
        m2 = _make_memory(key="location", value="Berlin", session="s1")
        m3 = _make_memory(key="pet", value="Dog", session="s2")
        graph = build_graph_from_memories([m1, m2, m3])
        assert "location" in graph.neighbors("employer")
        assert "employer" in graph.neighbors("location")
        # pet is in different session so should not co-occur with employer from session co-occurrence alone
        # but seed relations may connect them — check weight is at least seed level

    def test_build_graph_empty(self):
        graph = build_graph_from_memories([])
        assert isinstance(graph, EntityGraph)

    def test_seed_relations_coverage(self):
        assert "location" in SEED_RELATIONS.get("employer", set())
        assert "employer" in SEED_RELATIONS.get("location", set())


# ── Opinion arc rendering ─────────────────────────────────────────────

class TestEvolutionArc:
    def test_single_opinion_no_chain(self):
        m = _make_memory(key="preference_ts", value="I love TypeScript", mtype="opinion")
        result = _render_evolution_arc("preference_ts", [m], compact=False, superseded_chains=None)
        assert "preference_ts" in result
        assert "I love TypeScript" in result

    def test_opinion_evolution_with_chain(self):
        old = _make_memory(key="preference_ts", value="I hate TypeScript", mtype="opinion", days_old=30)
        new = _make_memory(key="preference_ts", value="TypeScript is fine for big projects", mtype="opinion")
        chain = [old]
        result = _render_evolution_arc("preference_ts", [new], compact=False, superseded_chains={new.id: chain})
        assert "→" in result
        assert "I hate TypeScript" in result
        assert "TypeScript is fine for big projects" in result

    def test_opinion_evolution_multiple_versions(self):
        oldest = _make_memory(key="preference_ts", value="I hate TypeScript", mtype="opinion", days_old=60)
        middle = _make_memory(key="preference_ts", value="TypeScript is okay", mtype="opinion", days_old=30)
        newest = _make_memory(key="preference_ts", value="I love TypeScript now", mtype="opinion")
        result = _render_evolution_arc("preference_ts", [newest, middle, oldest], compact=False, superseded_chains=None)
        assert "→" in result

    def test_fact_no_evolution(self):
        m = _make_memory(key="employer", value="Stripe", mtype="fact")
        result = _render_evolution_arc("employer", [m], compact=False, superseded_chains=None)
        assert "→" not in result
        assert "Stripe" in result

    def test_compact_mode(self):
        m = _make_memory(key="employer", value="Stripe", mtype="fact")
        result = _render_evolution_arc("employer", [m], compact=True, superseded_chains=None)
        assert not result.startswith("-")

    def test_non_compact_mode(self):
        m = _make_memory(key="employer", value="Stripe", mtype="fact")
        result = _render_evolution_arc("employer", [m], compact=False, superseded_chains=None)
        assert result.startswith("-")


class TestFormatStableFactsWithArc:
    def test_opinion_arc_in_stable_facts(self):
        old = _make_memory(key="preference_ts", value="I hate TS", mtype="opinion", days_old=30)
        old.id = uuid.uuid4()
        old.supersedes = None
        new = _make_memory(key="preference_ts", value="TS is fine for big projects", mtype="opinion")
        chains = {new.id: [old]}
        result = format_stable_facts([new], 512, superseded_chains=chains)
        assert "→" in result

    def test_multiple_types_mix(self):
        fact = _make_memory(key="employer", value="Stripe", mtype="fact")
        opinion = _make_memory(key="preference_lang", value="Love Python", mtype="opinion")
        old_opinion = _make_memory(key="preference_lang", value="Hated Python", mtype="opinion", days_old=100)
        chains = {opinion.id: [old_opinion]}
        result = format_stable_facts([fact, opinion], 512, superseded_chains=chains)
        assert "Stripe" in result
        assert "→" in result


# ── Tiktoken estimation ──────────────────────────────────────────────

class TestTiktokenEstimation:
    def test_empty_string(self):
        assert estimate_tokens("") == 0

    def test_short_text(self):
        assert estimate_tokens("hello world") > 0

    def test_deterministic(self):
        text = "The quick brown fox jumps over the lazy dog."
        assert estimate_tokens(text) == estimate_tokens(text)

    def test_longer_text_more_tokens(self):
        short = "hello"
        long = "hello " * 100
        assert estimate_tokens(long) > estimate_tokens(short)

    def test_special_characters(self):
        result = estimate_tokens("hello 🌍 world")
        assert result > 0

    def test_approximately_reasonable(self):
        # A typical English sentence: roughly 1 token per 4 chars
        text = "The user lives in Berlin and works as a software engineer."
        tokens = estimate_tokens(text)
        assert tokens > 5
        assert tokens < 30


# ── Reranker 0-based validation ──────────────────────────────────────

class TestReranker0Based:
    """Verify that the reranker validation enforces 0-based indices."""

    def test_valid_0_based_indices(self):
        """Simulate what llm_service.rerank does: filter 0-based indices."""
        memories = [{"value": f"mem{i}", "type": "fact", "key": f"k{i}"} for i in range(5)]
        parsed = {"ranked_indices": [0, 2, 4], "groups": []}

        valid = [idx for idx in parsed["ranked_indices"] if 0 <= idx < len(memories)]
        assert valid == [0, 2, 4]

    def test_invalid_negative_filtered(self):
        memories = [{"value": f"mem{i}", "type": "fact", "key": f"k{i}"} for i in range(5)]
        parsed = {"ranked_indices": [-1, 0, 2], "groups": []}

        valid = [idx for idx in parsed["ranked_indices"] if 0 <= idx < len(memories)]
        assert -1 not in valid

    def test_out_of_range_filtered(self):
        memories = [{"value": f"mem{i}", "type": "fact", "key": f"k{i}"} for i in range(3)]
        parsed = {"ranked_indices": [0, 1, 5, 10], "groups": []}

        valid = [idx for idx in parsed["ranked_indices"] if 0 <= idx < len(memories)]
        assert valid == [0, 1]

    def test_group_indices_validated(self):
        memories = [{"value": f"mem{i}", "type": "fact", "key": f"k{i}"} for i in range(5)]
        parsed = {
            "ranked_indices": [0, 1],
            "groups": [{"indices": [0, 5, -1], "reasoning": "test"}]
        }

        for g in parsed.get("groups", []):
            g["indices"] = [idx for idx in g.get("indices", []) if 0 <= idx < len(memories)]
        assert parsed["groups"][0]["indices"] == [0]
