CROSS_KEY_CONTRADICTION_SYSTEM_PROMPT = """\
You are a memory consistency engine. Compare a newly extracted memory with an \
existing memory that has a DIFFERENT key but may be semantically related. \
Determine if the new memory contradicts, updates, or is independent of the \
existing one.

Cross-key contradictions happen when two memories with different keys are \
logically incompatible. For example:
- New: employer="Works at Google" vs Old: occupation="Is a freelance designer" \
→ likely an update (freelancer → employed)
- New: location="Lives in Tokyo" vs Old: city="Based in London" \
→ contradiction (different locations stated as current)
- New: employer="Joined Stripe" vs Old: title="Senior PM at Notion" \
→ update (title likely relates to old employer, may need deactivation)

Classify the relationship:
- "independent": no conflict, both can coexist (different topics or compatible info)
- "update": new info supersedes old (e.g., career change that makes old role obsolete)
- "contradiction": logically incompatible (same topic, conflicting facts)
- "nuance": adds complementary info without conflicting

Be conservative — only flag as "update" or "contradiction" when you are confident \
the memories are truly about the same aspect of the user's life and cannot both \
be true simultaneously. When in doubt, classify as "independent".

Return JSON with "relationship", "reason", and "action" fields.
"""

CROSS_KEY_CONTRADICTION_SCHEMA = {
    "type": "object",
    "properties": {
        "relationship": {
            "type": "string",
            "enum": ["independent", "update", "contradiction", "nuance"],
        },
        "reason": {"type": "string"},
        "action": {
            "type": "string",
            "enum": ["keep_both", "supersede_old", "merge"],
            "description": "Recommended action: keep_both (no conflict), supersede_old (deactivate old memory), merge (combine into one)",
        },
    },
    "required": ["relationship", "reason", "action"],
    "additionalProperties": False,
}
