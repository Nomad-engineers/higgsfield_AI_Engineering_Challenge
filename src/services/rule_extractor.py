import re
import logging

logger = logging.getLogger(__name__)

PATTERNS = [
    (
        re.compile(
            r"I\s+(?:live|lived|moved\s+to)\s+(?:in|at|from)\s+(.+?)(?:\.|!|$)",
            re.IGNORECASE,
        ),
        "location",
        "fact",
    ),
    (
        re.compile(
            r"I\s+(?:work|worked|joined)\s+(?:at|for)\s+(.+?)(?:\.|!|$)",
            re.IGNORECASE,
        ),
        "employer",
        "fact",
    ),
    (
        re.compile(
            r"I(?:'m| am)\s+(?:allergic\s+to|have\s+an?\s+allergy\s+to)\s+(.+?)(?:\.|!|$)",
            re.IGNORECASE,
        ),
        "allergy",
        "fact",
    ),
    (
        re.compile(
            r"my\s+(dog|cat|pet)\s+(?:named|called)\s+(.+?)(?:\.|!|$)",
            re.IGNORECASE,
        ),
        "pet",
        "fact",
    ),
    (
        re.compile(
            r"I(?:'m| am)\s+(?:a|an)\s+(.+?)(?:\.|!|$)",
            re.IGNORECASE,
        ),
        "occupation",
        "fact",
    ),
    (
        re.compile(
            r"my\s+(?:name\s+(?:is|'s))\s+(.+?)(?:\.|!|$)",
            re.IGNORECASE,
        ),
        "name",
        "fact",
    ),
]


class RuleExtractor:
    def extract(self, messages: list[dict]) -> list[dict]:
        results = []
        seen = set()

        for msg in messages:
            content = msg.get("content", "")
            if not content or not isinstance(content, str):
                continue

            for pattern, key, type_ in PATTERNS:
                for match in pattern.finditer(content):
                    value = match.group(1).strip()
                    dedup_key = (key, value.lower())
                    if dedup_key in seen:
                        continue
                    seen.add(dedup_key)

                    if key == "pet":
                        pet_type = match.group(1).lower()
                        results.append({
                            "type": type_,
                            "key": key,
                            "value": f"Has a {pet_type} named {value}",
                            "confidence": 0.7,
                        })
                    else:
                        results.append({
                            "type": type_,
                            "key": key,
                            "value": value,
                            "confidence": 0.7,
                        })

        if results:
            logger.info(f"Rule-based extraction found {len(results)} memories")
        return results
