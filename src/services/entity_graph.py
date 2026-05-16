"""Dynamic entity graph for runtime entity linking.

Builds an adjacency graph from co-occurring keys within the same session/turn,
supplemented by a seed from static KEY_RELATIONS for cold-start coverage.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field


# Seed relations for cold start — same as the old KEY_RELATIONS but used as fallback only
SEED_RELATIONS: dict[str, set[str]] = {
    "pet": {"location", "name", "spouse", "child"},
    "employer": {"occupation", "title", "location", "education"},
    "spouse": {"spouse_occupation", "location", "child"},
    "child": {"location", "name", "spouse"},
    "location": {"employer", "pet", "name", "hobby"},
    "programming_language": {"framework", "employer"},
    "framework": {"programming_language", "employer"},
    "dietary_restriction": {"allergy"},
    "allergy": {"dietary_restriction"},
    "education": {"employer", "occupation"},
    "name": {"location", "employer"},
    "occupation": {"employer", "location"},
    "hobby": {"location", "occupation"},
    "vehicle": {"location", "employer"},
    "travel": {"location", "employer"},
    "mobile_framework": {"programming_language", "employer"},
    "sport": {"location", "hobby"},
    "music": {"hobby", "location"},
}

CO_OCCURRENCE_WEIGHT = 1.0
MIN_WEIGHT = 1.0


@dataclass
class EntityGraph:
    """Bidirectional weighted adjacency graph of memory keys."""
    edges: dict[str, dict[str, float]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(float)))

    def add_co_occurrence(self, key_a: str, key_b: str, weight: float = CO_OCCURRENCE_WEIGHT):
        if key_a == key_b:
            return
        self.edges[key_a][key_b] += weight
        self.edges[key_b][key_a] += weight

    def neighbors(self, key: str, min_weight: float = MIN_WEIGHT) -> set[str]:
        return {k for k, w in self.edges.get(key, {}).items() if w >= min_weight}

    def expand(self, keys: set[str], depth: int = 1, min_weight: float = MIN_WEIGHT) -> set[str]:
        """BFS expansion from seed keys, returning all reachable keys."""
        visited = set(keys)
        frontier = set(keys)
        for _ in range(depth):
            next_frontier = set()
            for key in frontier:
                for neighbor in self.neighbors(key, min_weight):
                    if neighbor not in visited:
                        next_frontier.add(neighbor)
                        visited.add(neighbor)
            frontier = next_frontier
            if not frontier:
                break
        return visited - keys

    def top_neighbors(self, key: str, limit: int = 5) -> list[tuple[str, float]]:
        neighbors = self.edges.get(key, {})
        return sorted(neighbors.items(), key=lambda x: x[1], reverse=True)[:limit]


def build_graph_from_memories(memories: list) -> EntityGraph:
    """Build entity graph from a list of memory objects.

    Co-occurrence is defined as: keys that share the same source_session
    AND have overlapping time windows (within same session).
    """
    graph = EntityGraph()

    # Seed with static relations for cold start
    for key, neighbors in SEED_RELATIONS.items():
        for n in neighbors:
            graph.add_co_occurrence(key, n, weight=0.5)

    if not memories:
        return graph

    # Group memories by session
    session_keys: dict[str, list[tuple[str, object]]] = defaultdict(list)
    for m in memories:
        session_keys[m.source_session].append((m.key, m))

    # Within each session, all active keys are co-occurring
    for session, key_list in session_keys.items():
        unique_keys = list({k for k, _ in key_list})
        for i in range(len(unique_keys)):
            for j in range(i + 1, len(unique_keys)):
                graph.add_co_occurrence(unique_keys[i], unique_keys[j])

    return graph
