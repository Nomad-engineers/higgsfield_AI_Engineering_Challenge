import re
import logging

logger = logging.getLogger(__name__)

KEY_ALIASES = {
    "company": "employer",
    "workplace": "employer",
    "city": "location",
    "hometown": "location",
    "job": "occupation",
    "role": "occupation",
    "position": "occupation",
    "title": "occupation",
    "diet": "dietary_restriction",
    "food_preference": "dietary_restriction",
}

OCCUPATION_STOP_WORDS = frozenset({
    "bit", "huge", "big", "little", "lot", "great", "avid", "really",
    "huge", "massive", "tiny", "bit", "bit", "little",
})

CORRECTION_KEY_PATTERNS = [
    (re.compile(r"(?i)\b(?:live|living|based|reside|moved)\s+"), "location"),
    (re.compile(r"(?i)\b(?:work|working|employed)\s+(?:at|for)"), "employer"),
    (re.compile(r"(?i)\bname\s+(?:is|'s)\s"), "name"),
    (re.compile(r"(?i)\b(?:allergic|intolerant)\s+to\s"), "allergy"),
    (re.compile(r"(?i)\b(?:vegetarian|vegan|pescatarian|keto|paleo|gluten)\b"), "dietary_restriction"),
    (re.compile(r"(?i)\b(?:have|got|own)\s+a\s+\w+\s+named"), "pet"),
    (re.compile(r"(?i)\b(?:prefer|like|want)\s+(?:my\s+)?(?:answer|response)"), "communication_style"),
    (re.compile(r"(?i)\b(?:love|hate|really\s+(?:like|dislike))\s"), "preference"),
]


def _infer_correction_key(text: str) -> str | None:
    for pattern, key in CORRECTION_KEY_PATTERNS:
        if pattern.search(text):
            return key
    return None


def normalize_key(key: str) -> str:
    return KEY_ALIASES.get(key, key)


PATTERNS = [
    # --- Location ---
    (
        re.compile(
            r"(?i)\bI\s+(?:live|am living|am based|reside|moved)\s+(?:in|to)\s+(.+?)(?:\.|,|!|$)"
        ),
        "location",
        "fact",
    ),
    (
        re.compile(
            r"(?i)\bI(?:'m| am)\s+from\s+(.+?)(?:\.|,|!|$)"
        ),
        "location",
        "fact",
    ),
    # --- Employment ---
    (
        re.compile(
            r"(?i)\bI\s+(?:work|am working|am employed)\s+(?:at|for|with)\s+(.+?)(?:\.|,|!|$)"
        ),
        "employer",
        "fact",
    ),
    (
        re.compile(
            r"(?i)\bI\s+(?:just\s+)?(?:joined|started|hired|got a job)\s+(?:at\s+)?(.+?)(?:\.|,|!|$)"
        ),
        "employer",
        "fact",
    ),
    (
        re.compile(
            r"(?i)\bmy\s+(?:job|role|position|title)\s+(?:is|was)\s+(.+?)(?:\.|,|!|$)"
        ),
        "occupation",
        "fact",
    ),
    # --- Pets ---
    (
        re.compile(
            r"(?i)\bI\s+(?:have|got|own)\s+a\s+(\w+)\s+(?:named|called)\s+(\w+)"
        ),
        "pet",
        "fact",
    ),
    (
        re.compile(
            r"(?i)\bmy\s+(\w+)\s+(?:named|called)\s+(\w+)"
        ),
        "pet",
        "fact",
    ),
    (
        re.compile(
            r"(?i)\b(?:walking|feeding|playing\s+with)\s+(?:my\s+)?(\w+)\s+(?:named\s+)?(\w+)"
        ),
        "pet",
        "fact",
    ),
    # --- Allergies ---
    (
        re.compile(
            r"(?i)\bI(?:'m| am)\s+(?:allergic|intolerant)\s+to\s+(.+?)(?:\.|,|!|$)"
        ),
        "allergy",
        "fact",
    ),
    # --- Diet ---
    (
        re.compile(
            r"(?i)\bI(?:'m| am)\s+(vegetarian|vegan|pescatarian|keto|paleo|gluten[\s-]free)\b"
        ),
        "dietary_restriction",
        "fact",
    ),
    (
        re.compile(
            r"(?i)\bI\s+(?:don't|do not)\s+eat\s+(.+?)(?:\.|,|!|$)"
        ),
        "dietary_restriction",
        "fact",
    ),
    # --- Communication style ---
    (
        re.compile(
            r"(?i)\bI\s+(?:prefer|like|want)\s+(?:my\s+)?(?:answers?|responses?|replies?)\s+(?:to be\s+)?(.+?)(?:\.|,|!|$)"
        ),
        "communication_style",
        "preference",
    ),
    (
        re.compile(
            r"(?i)\bplease\s+be\s+(concise|detailed|brief|short|direct|formal|casual)"
        ),
        "communication_style",
        "preference",
    ),
    # --- Preferences (opinions) ---
    (
        re.compile(
            r"(?i)\bI\s+(?:love|hate|really\s+(?:like|dislike))\s+(.+?)(?:\.|,|!|$)"
        ),
        "preference",
        "preference",
    ),
    # --- Name ---
    (
        re.compile(
            r"(?i)\bmy\s+name\s+(?:is|'s)\s+(.+?)(?:\.|,|!|$)"
        ),
        "name",
        "fact",
    ),
    # --- Correction ---
    (
        re.compile(
            r"(?i)\b(?:actually|sorry|i meant)\s*[,:]?\s*(?:not\s+)?(.+?)(?:\.|,|!|$)"
        ),
        "correction",
        "fact",
    ),
    # --- Fallback occupation ---
    (
        re.compile(
            r"(?i)\bI(?:'m| am)\s+(?:a|an)\s+(.+?)(?:\.|,|!|$)"
        ),
        "occupation",
        "fact",
    ),
]

HIGH_CONFIDENCE_KEYS = {"employer", "location", "name", "allergy"}


def _confidence_for_match(key: str, value: str) -> float:
    base = 0.7
    if len(value) > 20:
        base += 0.05
    if key in HIGH_CONFIDENCE_KEYS:
        base += 0.1
    return min(base, 0.85)


class RuleExtractor:
    def extract(self, messages: list[dict]) -> list[dict]:
        results = []
        seen = set()

        for msg in messages:
            if msg.get("role") != "user":
                continue

            content = msg.get("content", "")
            if not content or not isinstance(content, str):
                continue

            for pattern, key, type_ in PATTERNS:
                for match in pattern.finditer(content):
                    groups = [g for g in match.groups() if g]
                    if not groups:
                        continue

                    if key == "pet" and len(groups) >= 2:
                        pet_type = groups[0].lower()
                        pet_name = groups[1].strip()
                        value = f"Has a {pet_type} named {pet_name}"
                        dedup_key = (key, pet_name.lower())
                    else:
                        value = groups[0].strip()
                        dedup_key = (key, value.lower())

                    # Skip false occupation matches
                    if key == "occupation" and value.split()[0].lower() in OCCUPATION_STOP_WORDS:
                        continue

                    # For corrections, infer the real key from the corrected text
                    if key == "correction":
                        inferred = _infer_correction_key(value)
                        if inferred:
                            key = inferred
                            dedup_key = (key, value.lower())

                    if dedup_key in seen:
                        continue
                    seen.add(dedup_key)

                    results.append({
                        "type": type_,
                        "key": key,
                        "value": value,
                        "confidence": _confidence_for_match(key, value),
                    })

        if results:
            logger.info(f"Rule-based extraction found {len(results)} memories")
        return results
