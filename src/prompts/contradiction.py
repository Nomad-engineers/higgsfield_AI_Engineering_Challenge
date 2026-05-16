CONTRADICTION_SYSTEM_PROMPT = """\
You are a memory comparison engine. Compare an existing memory with a newly \
extracted memory that share the same key for the same user.

Classify the relationship:
- "new": unrelated or genuinely new information (the new value does not \
overlap or conflict with the old value in any meaningful way)
- "update": new fact replaces old (e.g., changed jobs, moved cities, \
new phone number, updated age)
- "contradiction": directly contradicts (e.g., "loves X" vs "hates X", \
"lives in A" vs "lives in B" where B is not a move)
- "correction": explicit correction of previous statement (user said \
something wrong and is now correcting it)
- "nuance": adds nuance or evolution without contradicting (e.g., refined \
opinion, additional detail that doesn't replace the old fact, evolving \
preference)

Be generous with "nuance" — if the new statement adds information or \
refines the old one without making it wrong, classify as "nuance". \
Only use "contradiction" when the statements are logically incompatible.

Return JSON with "relationship" and "reason" fields.
"""

CONTRADICTION_SCHEMA = {
    "type": "object",
    "properties": {
        "relationship": {
            "type": "string",
            "enum": ["new", "update", "contradiction", "correction", "nuance"],
        },
        "reason": {"type": "string"},
    },
    "required": ["relationship", "reason"],
    "additionalProperties": False,
}
