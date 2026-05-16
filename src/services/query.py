"""Query hint vocabulary — maps natural-language patterns to canonical memory keys.

Improves BM25 recall by expanding queries with synonyms and identifying target keys
for direct SQL matching, without requiring embeddings or LLM calls.
"""
import re

# Pattern -> (canonical_key, extra_search_terms)
HINT_PATTERNS: list[tuple[re.Pattern, str, list[str]]] = [
    # Location / city
    (re.compile(r"\b(where .*(?:live|reside|stay|based)|location|city|address|home town|from)\b", re.I),
     "location", ["city", "country", "address", "live", "lives", "lived", "residence", "based"]),
    # Pet / animal
    (re.compile(r"\b(pets?|dog|cat|bird|fish|hamster|rabbit|animal|puppy|kitten|breed|golden retriever)\b", re.I),
     "pet", ["dog", "cat", "bird", "fish", "animal", "pet name", "breed"]),
    # Employer / work / job
    (re.compile(r"\b(employer|work(?:s|ing|ed)?|job|company|occupation|title|career|profession|does for (?:a )?living|hired|industry|what .{0,10} do)\b", re.I),
     "employer", ["company", "job", "work", "career", "occupation", "title", "position", "role"]),
    # Food / diet / allergies
    (re.compile(r"\b(food|diet|dietary|eat|allerg|vegetarian|vegan|gluten|shellfish|meal|cuisine|restaurant|cook|restriction|preference)\b", re.I),
     "food_preferences", ["food", "eat", "diet", "dietary", "allergy", "allergies", "vegetarian", "vegan", "meal"]),
    # Programming / languages / tech
    (re.compile(r"\b(programming|languages?|framework|tech stack|code|coding|developer|software|python|typescript|javascript|react|rust|java)\b", re.I),
     "programming", ["programming", "language", "framework", "tech", "code", "developer", "software"]),
    # Spouse / partner / relationship
    (re.compile(r"\b(spouse|partner|husband|wife|married|significant other|fianc)\b", re.I),
     "spouse", ["spouse", "partner", "husband", "wife", "married", "relationship"]),
    # Travel / trips
    (re.compile(r"\b(travel|trip|vacation|visit|journey|flown|flew|flew to|went to|been to)\b", re.I),
     "travel", ["travel", "trip", "vacation", "visit", "journey", "country"]),
    # Birthday / age / born
    (re.compile(r"\b(birthday|born|age|date of birth|birthdate|years old)\b", re.I),
     "birthday", ["birthday", "born", "age", "date of birth"]),
    # Mobile / app framework
    (re.compile(r"\b(mobile|app|ios|android|react native|flutter|swift|kotlin)\b", re.I),
     "mobile_framework", ["mobile", "app", "ios", "android", "framework", "react native"]),
    # Hobbies / interests
    (re.compile(r"\b(hobb|interest|pastime|enjoy|leisure|free time|fun|passion)\b", re.I),
     "hobbies", ["hobby", "interest", "pastime", "leisure", "enjoy"]),
    # Education / school / degree
    (re.compile(r"\b(education|school|university|college|degree|stud(y|ied|ies)|graduated|alumni|academic)\b", re.I),
     "education", ["school", "university", "college", "degree", "education", "study"]),
    # Name
    (re.compile(r"\b(name|called|known as|who is)\b", re.I),
     "name", ["name", "called"]),
    # Car / vehicle
    (re.compile(r"\b(car|vehicle|drive|drives|automobile|auto)\b", re.I),
     "car", ["car", "vehicle", "drive", "automobile"]),
    # Music / instrument
    (re.compile(r"\b(music|instrument|play|band|concert|song|genre|artist|listen)\b", re.I),
     "music", ["music", "instrument", "band", "concert", "song"]),
    # Sport / exercise
    (re.compile(r"\b(sport|exercise|gym|workout|run|fitness|athletic|team|play(?:s|ing)?)\b", re.I),
     "sport", ["sport", "exercise", "gym", "fitness", "athletic"]),
]


def analyze_query(query: str) -> dict:
    """Analyze a query and return hint keys and expanded search terms.

    Returns:
        {
            "hint_keys": set of canonical memory keys to boost / match directly,
            "expanded_terms": list of additional search terms for BM25,
            "primary_key": the best-matching canonical key (or None),
        }
    """
    hint_keys = set()
    expanded_terms = []
    primary_key = None
    best_match_len = 0

    for pattern, key, terms in HINT_PATTERNS:
        match = pattern.search(query)
        if match:
            hint_keys.add(key)
            expanded_terms.extend(terms)
            match_len = match.end() - match.start()
            if match_len > best_match_len:
                best_match_len = match_len
                primary_key = key

    return {
        "hint_keys": hint_keys,
        "expanded_terms": expanded_terms,
        "primary_key": primary_key,
    }


def expand_query_for_bm25(query: str) -> str:
    """Return a query string augmented with hint vocabulary terms for BM25."""
    hints = analyze_query(query)
    if not hints["expanded_terms"]:
        return query
    extra = " ".join(hints["expanded_terms"])
    return f"{query} {extra}"
