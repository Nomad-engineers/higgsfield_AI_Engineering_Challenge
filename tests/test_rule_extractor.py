import pytest

from src.services.rule_extractor import RuleExtractor, normalize_key, _confidence_for_match


class TestSubjectGate:
    def test_extracts_from_user_only(self):
        messages = [
            {"role": "assistant", "content": "I live in Berlin"},
            {"role": "user", "content": "I live in Tokyo"},
        ]
        results = RuleExtractor().extract(messages)
        assert len(results) == 1
        assert results[0]["key"] == "location"
        assert "Tokyo" in results[0]["value"]

    def test_ignores_system_messages(self):
        messages = [
            {"role": "system", "content": "I live in Berlin"},
            {"role": "user", "content": "Hello"},
        ]
        results = RuleExtractor().extract(messages)
        assert results == []

    def test_ignores_empty_content(self):
        messages = [
            {"role": "user", "content": ""},
            {"role": "user", "content": None},
        ]
        results = RuleExtractor().extract(messages)
        assert results == []


class TestLocationPatterns:
    def test_live_in(self):
        results = RuleExtractor().extract([
            {"role": "user", "content": "I live in Berlin."}
        ])
        assert len(results) == 1
        assert results[0]["key"] == "location"
        assert "Berlin" in results[0]["value"]

    def test_am_living_in(self):
        results = RuleExtractor().extract([
            {"role": "user", "content": "I am living in Paris!"}
        ])
        assert len(results) == 1
        assert "Paris" in results[0]["value"]

    def test_moved_to(self):
        results = RuleExtractor().extract([
            {"role": "user", "content": "I moved to London."}
        ])
        assert len(results) == 1
        assert "London" in results[0]["value"]

    def test_am_from(self):
        results = RuleExtractor().extract([
            {"role": "user", "content": "I'm from Tokyo."}
        ])
        assert len(results) == 1
        assert "Tokyo" in results[0]["value"]


class TestEmploymentPatterns:
    def test_work_at(self):
        results = RuleExtractor().extract([
            {"role": "user", "content": "I work at Google."}
        ])
        assert len(results) == 1
        assert results[0]["key"] == "employer"
        assert "Google" in results[0]["value"]

    def test_joined(self):
        results = RuleExtractor().extract([
            {"role": "user", "content": "I just joined Meta."}
        ])
        assert len(results) == 1
        assert results[0]["key"] == "employer"
        assert "Meta" in results[0]["value"]

    def test_my_role_is(self):
        results = RuleExtractor().extract([
            {"role": "user", "content": "My role is software engineer."}
        ])
        assert len(results) == 1
        assert results[0]["key"] == "occupation"
        assert "software engineer" in results[0]["value"]


class TestPetPatterns:
    def test_have_a_dog_named(self):
        results = RuleExtractor().extract([
            {"role": "user", "content": "I have a dog named Biscuit."}
        ])
        assert len(results) == 1
        assert results[0]["key"] == "pet"
        assert "Biscuit" in results[0]["value"]

    def test_my_cat_named(self):
        results = RuleExtractor().extract([
            {"role": "user", "content": "My cat named Whiskers is cute."}
        ])
        assert len(results) == 1
        assert results[0]["key"] == "pet"
        assert "Whiskers" in results[0]["value"]

    def test_walking_my_dog_named(self):
        results = RuleExtractor().extract([
            {"role": "user", "content": "I was walking my dog named Rex yesterday."}
        ])
        assert len(results) == 1
        assert results[0]["key"] == "pet"
        assert "Rex" in results[0]["value"]


class TestAllergyPattern:
    def test_allergic_to(self):
        results = RuleExtractor().extract([
            {"role": "user", "content": "I'm allergic to peanuts."}
        ])
        assert len(results) == 1
        assert results[0]["key"] == "allergy"
        assert "peanuts" in results[0]["value"]

    def test_intolerant_to(self):
        results = RuleExtractor().extract([
            {"role": "user", "content": "I am intolerant to lactose."}
        ])
        assert len(results) == 1
        assert results[0]["key"] == "allergy"


class TestDietPatterns:
    def test_vegetarian(self):
        results = RuleExtractor().extract([
            {"role": "user", "content": "I'm vegetarian."}
        ])
        assert len(results) == 1
        assert results[0]["key"] == "dietary_restriction"
        assert "vegetarian" in results[0]["value"]

    def test_dont_eat(self):
        results = RuleExtractor().extract([
            {"role": "user", "content": "I don't eat meat."}
        ])
        assert len(results) == 1
        assert results[0]["key"] == "dietary_restriction"
        assert "meat" in results[0]["value"]


class TestCommunicationStyle:
    def test_prefer_concise(self):
        results = RuleExtractor().extract([
            {"role": "user", "content": "I prefer my answers to be concise."}
        ])
        assert len(results) == 1
        assert results[0]["key"] == "communication_style"
        assert results[0]["type"] == "preference"

    def test_please_be_direct(self):
        results = RuleExtractor().extract([
            {"role": "user", "content": "Please be direct."}
        ])
        assert len(results) == 1
        assert results[0]["key"] == "communication_style"


class TestPreferencePattern:
    def test_love(self):
        results = RuleExtractor().extract([
            {"role": "user", "content": "I love chocolate."}
        ])
        assert len(results) == 1
        assert results[0]["key"] == "preference"
        assert results[0]["type"] == "preference"

    def test_hate(self):
        results = RuleExtractor().extract([
            {"role": "user", "content": "I hate waking up early."}
        ])
        assert len(results) == 1
        assert results[0]["key"] == "preference"


class TestNamePattern:
    def test_my_name_is(self):
        results = RuleExtractor().extract([
            {"role": "user", "content": "My name is Alice."}
        ])
        assert len(results) == 1
        assert results[0]["key"] == "name"
        assert "Alice" in results[0]["value"]


class TestCorrectionPattern:
    def test_actually(self):
        results = RuleExtractor().extract([
            {"role": "user", "content": "Actually, I live in Munich."}
        ])
        keys = {r["key"] for r in results}
        assert "correction" in keys


class TestKeyNormalization:
    def test_company_to_employer(self):
        assert normalize_key("company") == "employer"

    def test_workplace_to_employer(self):
        assert normalize_key("workplace") == "employer"

    def test_city_to_location(self):
        assert normalize_key("city") == "location"

    def test_employer_unchanged(self):
        assert normalize_key("employer") == "employer"

    def test_unknown_passthrough(self):
        assert normalize_key("custom_key") == "custom_key"


class TestConfidenceScoring:
    def test_high_confidence_keys_get_bonus(self):
        c = _confidence_for_match("employer", "Google")
        assert c == pytest.approx(0.8, abs=0.01)

    def test_normal_keys_base(self):
        c = _confidence_for_match("preference", "something")
        assert c == 0.7

    def test_long_value_gets_bonus(self):
        short = _confidence_for_match("preference", "short")
        long = _confidence_for_match("preference", "a" * 25)
        assert long > short

    def test_max_confidence_capped(self):
        c = _confidence_for_match("name", "a" * 30)
        assert c <= 0.85


class TestDedup:
    def test_no_duplicate_same_key_same_value(self):
        results = RuleExtractor().extract([
            {"role": "user", "content": "I live in Berlin. I live in Berlin."},
        ])
        locations = [r for r in results if r["key"] == "location"]
        assert len(locations) == 1

    def test_different_values_same_key_both_captured(self):
        results = RuleExtractor().extract([
            {"role": "user", "content": "I live in Berlin. I live in Paris."},
        ])
        locations = [r for r in results if r["key"] == "location"]
        assert len(locations) == 2

    def test_multiple_patterns_from_one_message(self):
        results = RuleExtractor().extract([
            {"role": "user", "content": "I live in Berlin. I work at Google."},
        ])
        keys = {r["key"] for r in results}
        assert "location" in keys
        assert "employer" in keys


class TestMergeExtractions:
    """Test merge logic without importing ExtractionService (avoids sqlalchemy dependency)."""

    @staticmethod
    def _merge_extractions(rules, llm):
        merged = {}
        for r in rules:
            key = normalize_key(r["key"])
            merged[key] = {**r, "key": key}
        for r in llm:
            key = normalize_key(r.get("key", ""))
            if not key:
                continue
            merged[key] = {**r, "key": key}
        return list(merged.values())

    def test_rules_fill_gaps(self):
        rules = [{"type": "fact", "key": "location", "value": "Berlin", "confidence": 0.7}]
        merged = self._merge_extractions(rules, [])
        assert len(merged) == 1
        assert merged[0]["value"] == "Berlin"

    def test_llm_wins_on_conflict(self):
        rules = [{"type": "fact", "key": "location", "value": "Berlin (rule)", "confidence": 0.7}]
        llm = [{"type": "fact", "key": "location", "value": "Berlin, Germany (LLM)", "confidence": 0.9}]
        merged = self._merge_extractions(rules, llm)
        assert len(merged) == 1
        assert "LLM" in merged[0]["value"]

    def test_both_contribute_unique_keys(self):
        rules = [{"type": "fact", "key": "location", "value": "Berlin", "confidence": 0.7}]
        llm = [{"type": "fact", "key": "employer", "value": "Google", "confidence": 0.9}]
        merged = self._merge_extractions(rules, llm)
        assert len(merged) == 2

    def test_rules_only_when_no_llm(self):
        rules = [
            {"type": "fact", "key": "location", "value": "Berlin", "confidence": 0.7},
            {"type": "fact", "key": "name", "value": "Alice", "confidence": 0.8},
        ]
        merged = self._merge_extractions(rules, [])
        assert len(merged) == 2

    def test_key_normalization_during_merge(self):
        rules = [{"type": "fact", "key": "company", "value": "Google (rule)", "confidence": 0.7}]
        llm = [{"type": "fact", "key": "employer", "value": "Google (LLM)", "confidence": 0.9}]
        merged = self._merge_extractions(rules, llm)
        assert len(merged) == 1
        assert merged[0]["key"] == "employer"

    def test_llm_empty_key_skipped(self):
        rules = [{"type": "fact", "key": "location", "value": "Berlin", "confidence": 0.7}]
        llm = [{"type": "fact", "key": "", "value": "noise", "confidence": 0.5}]
        merged = self._merge_extractions(rules, llm)
        assert len(merged) == 1
        assert merged[0]["key"] == "location"
