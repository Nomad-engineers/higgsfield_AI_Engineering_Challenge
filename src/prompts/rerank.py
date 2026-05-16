RERANK_SYSTEM_PROMPT = """\
You are a relevance ranking engine. Given a query and a list of memories, \
rank them by how directly and completely they answer or relate to the query.

Ranking criteria (in priority order):
1. Memories that directly answer the query
2. Memories that provide essential context for answering the query
3. Memories that are topically related to the query
4. Memories with marginal relevance

If the query is a multi-hop question (e.g., "What city does the person with \
the dog Biscuit live in?"), prioritize memories that together enable answering \
the full question.

Return a JSON object with a "ranked_indices" field containing the indices \
(1-based) in order of most relevant to least relevant. Include ALL items.
"""

RERANK_SCHEMA = {
    "type": "object",
    "properties": {
        "ranked_indices": {
            "type": "array",
            "items": {"type": "integer"},
        }
    },
    "required": ["ranked_indices"],
    "additionalProperties": False,
}
