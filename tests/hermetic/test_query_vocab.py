"""Hermetic tests for query hint vocabulary."""
import pytest

from src.services.query import analyze_query, expand_query_for_bm25


class TestAnalyzeQuery:
    def test_location_query(self):
        result = analyze_query("Where does this user live?")
        assert "location" in result["hint_keys"]
        assert result["primary_key"] == "location"

    def test_pet_query(self):
        result = analyze_query("What is the user's dog's name?")
        assert "pet" in result["hint_keys"]

    def test_employer_query(self):
        result = analyze_query("What does this user do for work?")
        assert "employer" in result["hint_keys"]

    def test_food_query(self):
        result = analyze_query("Does this user have any dietary restrictions?")
        assert "food_preferences" in result["hint_keys"]

    def test_programming_query(self):
        result = analyze_query("What programming languages does the user prefer?")
        assert "programming" in result["hint_keys"]

    def test_spouse_query(self):
        result = analyze_query("What's the user's spouse's name?")
        assert "spouse" in result["hint_keys"]

    def test_travel_query(self):
        result = analyze_query("Where did the user travel recently?")
        assert "travel" in result["hint_keys"]

    def test_birthday_query(self):
        result = analyze_query("When is the user's birthday?")
        assert "birthday" in result["hint_keys"]

    def test_mobile_framework_query(self):
        result = analyze_query("What framework is the user's mobile app built with?")
        assert "mobile_framework" in result["hint_keys"]

    def test_multi_hop_query(self):
        result = analyze_query("Where does Bob live and what does he do?")
        assert "location" in result["hint_keys"]
        assert "employer" in result["hint_keys"] or "name" in result["hint_keys"]

    def test_car_query(self):
        result = analyze_query("What car does the user drive?")
        assert "car" in result["hint_keys"]
        assert "location" not in result["hint_keys"]

    def test_unrelated_query(self):
        result = analyze_query("What's the capital of France?")
        assert len(result["hint_keys"]) == 0
        assert result["primary_key"] is None

    def test_work_occupation_synonym(self):
        result = analyze_query("What is the user's occupation?")
        assert "employer" in result["hint_keys"]

    def test_pet_cat(self):
        result = analyze_query("Tell me about the user's cat")
        assert "pet" in result["hint_keys"]

    def test_vegetarian_matches_food(self):
        result = analyze_query("Is the user vegetarian?")
        assert "food_preferences" in result["hint_keys"]

    def test_expanded_terms_not_empty_when_matched(self):
        result = analyze_query("Where does this user live?")
        assert len(result["expanded_terms"]) > 0
        assert "city" in result["expanded_terms"]


class TestExpandQueryForBM25:
    def test_expands_location_query(self):
        expanded = expand_query_for_bm25("Where does this user live?")
        assert len(expanded) > len("Where does this user live?")
        assert "city" in expanded

    def test_no_expansion_for_unrelated(self):
        query = "What's the capital of France?"
        expanded = expand_query_for_bm25(query)
        assert expanded == query

    def test_expansion_preserves_original(self):
        query = "What is the user's dog's name?"
        expanded = expand_query_for_bm25(query)
        assert query in expanded
        assert "dog" in expanded
