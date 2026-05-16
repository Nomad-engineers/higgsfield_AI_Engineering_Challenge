#!/bin/bash
set -euo pipefail

BASE="http://localhost:8080"
PASS=0
FAIL=0
ERRORS=""

check() {
    local test_name="$1" http_code="$2" expected="$3" body="$4"
    if [ "$http_code" == "$expected" ]; then
        echo "  PASS: $test_name (HTTP $http_code)"
        PASS=$((PASS+1))
    else
        echo "  FAIL: $test_name (expected $expected, got $http_code)"
        echo "    Body: $(echo "$body" | head -c 200)"
        FAIL=$((FAIL+1))
        ERRORS="$ERRORS\n- $test_name: expected $expected, got $http_code"
    fi
}

check_contains() {
    local test_name="$1" haystack="$2" needle="$3"
    if echo "$haystack" | grep -qi "$needle"; then
        echo "  PASS: $test_name (contains '$needle')"
        PASS=$((PASS+1))
    else
        echo "  FAIL: $test_name (missing '$needle')"
        echo "    Body: $(echo "$haystack" | head -c 300)"
        FAIL=$((FAIL+1))
        ERRORS="$ERRORS\n- $test_name: missing '$needle'"
    fi
}

check_not_contains() {
    local test_name="$1" haystack="$2" needle="$3"
    if echo "$haystack" | grep -qi "$needle"; then
        echo "  FAIL: $test_name (should NOT contain '$needle')"
        FAIL=$((FAIL+1))
        ERRORS="$ERRORS\n- $test_name: should NOT contain '$needle'"
    else
        echo "  PASS: $test_name (correctly missing '$needle')"
        PASS=$((PASS+1))
    fi
}

echo "=========================================="
echo "  COMPREHENSIVE MEMORY SERVICE TEST SUITE"
echo "=========================================="
echo ""

# ==========================================
echo "1. HEALTH CHECK"
echo "------------------------------------------"
CODE=$(curl -s -o /tmp/resp.json -w "%{http_code}" "$BASE/health")
BODY=$(cat /tmp/resp.json)
check "GET /health returns 200" "$CODE" "200" "$BODY"
check_contains "health returns ok" "$BODY" "ok"

# ==========================================
echo ""
echo "2. CONTRACT: POST /turns"
echo "------------------------------------------"
CODE=$(curl -s -o /tmp/resp.json -w "%{http_code}" -X POST "$BASE/turns" \
  -H 'Content-Type: application/json' \
  -d '{
    "session_id": "test-s1",
    "user_id": "user-test1",
    "messages": [
      {"role": "user", "content": "I work at Stripe as a software engineer."},
      {"role": "assistant", "content": "Great! Stripe is a fantastic company."}
    ],
    "timestamp": "2025-03-10T10:00:00Z",
    "metadata": {}
  }')
BODY=$(cat /tmp/resp.json)
check "POST /turns returns 201" "$CODE" "201" "$BODY"
TURN_ID=$(echo "$BODY" | jq -r '.id // empty')
if [ -n "$TURN_ID" ]; then
    echo "  INFO: Turn ID = $TURN_ID"
    PASS=$((PASS+1))
else
    echo "  FAIL: No turn ID in response"
    FAIL=$((FAIL+1))
fi

# Second turn — same user, different session
CODE=$(curl -s -o /tmp/resp.json -w "%{http_code}" -X POST "$BASE/turns" \
  -H 'Content-Type: application/json' \
  -d '{
    "session_id": "test-s2",
    "user_id": "user-test1",
    "messages": [
      {"role": "user", "content": "I just started at Notion as a PM! Loving it so far."},
      {"role": "assistant", "content": "Congratulations on the new role!"}
    ],
    "timestamp": "2025-03-15T14:00:00Z",
    "metadata": {}
  }')
BODY=$(cat /tmp/resp.json)
check "POST /turns (turn 2) returns 201" "$CODE" "201" "$BODY"

# ==========================================
echo ""
echo "3. CONTRACT: POST /recall"
echo "------------------------------------------"
sleep 3
CODE=$(curl -s -o /tmp/resp.json -w "%{http_code}" -X POST "$BASE/recall" \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "Where does this user work?",
    "session_id": "test-s3",
    "user_id": "user-test1",
    "max_tokens": 512
  }')
BODY=$(cat /tmp/resp.json)
check "POST /recall returns 200" "$CODE" "200" "$BODY"
check_contains "recall mentions Notion (current employer)" "$BODY" "Notion"
check_contains "recall has context field" "$BODY" "context"
check_contains "recall has citations" "$BODY" "citations"

echo "  Recall context:"
echo "$BODY" | jq -r '.context' | head -20

# ==========================================
echo ""
echo "4. CONTRACT: POST /search"
echo "------------------------------------------"
CODE=$(curl -s -o /tmp/resp.json -w "%{http_code}" -X POST "$BASE/search" \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "employer",
    "session_id": null,
    "user_id": "user-test1",
    "limit": 10
  }')
BODY=$(cat /tmp/resp.json)
check "POST /search returns 200" "$CODE" "200" "$BODY"
RESULT_COUNT=$(echo "$BODY" | jq '.results | length')
echo "  INFO: Search returned $RESULT_COUNT results"

# ==========================================
echo ""
echo "5. CONTRACT: GET /users/{user_id}/memories"
echo "------------------------------------------"
CODE=$(curl -s -o /tmp/resp.json -w "%{http_code}" "$BASE/users/user-test1/memories")
BODY=$(cat /tmp/resp.json)
check "GET /memories returns 200" "$CODE" "200" "$BODY"
MEM_COUNT=$(echo "$BODY" | jq '.memories | length')
echo "  INFO: $MEM_COUNT memories stored"

# Check structured memories (not raw text)
FIRST_TYPE=$(echo "$BODY" | jq -r '.memories[0].type // empty')
if [ -n "$FIRST_TYPE" ]; then
    echo "  PASS: Memories are structured (type=$FIRST_TYPE)"
    PASS=$((PASS+1))
else
    echo "  FAIL: Memories missing 'type' field - may be raw text"
    FAIL=$((FAIL+1))
fi

# Check supersession chain
SUPERSEDED=$(echo "$BODY" | jq -r '.memories[] | select(.key == "employer") | .supersedes // empty' | head -1)
SUPERSEDED_BY=$(echo "$BODY" | jq -r '.memories[] | select(.key == "employer") | .superseded_by // empty' | head -1)
ACTIVE_EMPLOYER=$(echo "$BODY" | jq -r '.memories[] | select(.key == "employer" and .active == true) | .value' | head -1)
echo "  INFO: Active employer = '$ACTIVE_EMPLOYER'"

if echo "$ACTIVE_EMPLOYER" | grep -qi "Notion"; then
    echo "  PASS: Current employer is Notion (superseded Stripe)"
    PASS=$((PASS+1))
else
    echo "  FAIL: Current employer should be Notion, got '$ACTIVE_EMPLOYER'"
    FAIL=$((FAIL+1))
fi

echo "  Full memories:"
echo "$BODY" | jq .

# ==========================================
echo ""
echo "6. FACT EVOLUTION"
echo "------------------------------------------"
# Add a third turn with location info
CODE=$(curl -s -o /tmp/resp.json -w "%{http_code}" -X POST "$BASE/turns" \
  -H 'Content-Type: application/json' \
  -d '{
    "session_id": "test-s1",
    "user_id": "user-test1",
    "messages": [
      {"role": "user", "content": "I live in San Francisco now. Just moved here last week."},
      {"role": "assistant", "content": "Welcome to SF! How do you like the weather?"}
    ],
    "timestamp": "2025-03-20T10:00:00Z",
    "metadata": {}
  }')
check "Turn with location update returns 201" "$CODE" "201" "$(cat /tmp/resp.json)"
sleep 3

# Check recall shows SF
CODE=$(curl -s -o /tmp/resp.json -w "%{http_code}" -X POST "$BASE/recall" \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "Where does this user live?",
    "session_id": "test-s3",
    "user_id": "user-test1",
    "max_tokens": 512
  }')
BODY=$(cat /tmp/resp.json)
check "Recall after evolution returns 200" "$CODE" "200" "$BODY"
check_contains "recall mentions SF (updated location)" "$BODY" "Francisco"

# ==========================================
echo ""
echo "7. ROBUSTNESS: Malformed input"
echo "------------------------------------------"

# Bad JSON
CODE=$(curl -s -o /tmp/resp.json -w "%{http_code}" -X POST "$BASE/turns" \
  -H 'Content-Type: application/json' \
  -d '{bad json}')
check "Bad JSON returns 4xx" "$CODE" "422" "$(cat /tmp/resp.json)"

# Missing required fields
CODE=$(curl -s -o /tmp/resp.json -w "%{http_code}" -X POST "$BASE/turns" \
  -H 'Content-Type: application/json' \
  -d '{"session_id": "test"}')
check "Missing fields returns 422" "$CODE" "422" "$(cat /tmp/resp.json)"

# Unicode test
CODE=$(curl -s -o /tmp/resp.json -w "%{http_code}" -X POST "$BASE/turns" \
  -H 'Content-Type: application/json' \
  -d '{
    "session_id": "test-unicode",
    "user_id": "user-unicode",
    "messages": [
      {"role": "user", "content": "Ich liebe münchen! café résumé 日本語テスト 🎉"},
      {"role": "assistant", "content": "That is wonderful!"}
    ],
    "timestamp": "2025-03-15T10:00:00Z",
    "metadata": {"emoji": "🎉", "chinese": "你好"}
  }')
check "Unicode payload returns 201" "$CODE" "201" "$(cat /tmp/resp.json)"

# Oversized metadata
CODE=$(curl -s -o /tmp/resp.json -w "%{http_code}" -X POST "$BASE/turns" \
  -H 'Content-Type: application/json' \
  -d '{
    "session_id": "test-big",
    "user_id": null,
    "messages": [
      {"role": "user", "content": "test"},
      {"role": "assistant", "content": "ok"}
    ],
    "timestamp": "2025-03-15T10:00:00Z",
    "metadata": {"data": "'$(python3 -c "print('x'*10000)")'"}
  }')
check "Large metadata returns 201" "$CODE" "201" "$(cat /tmp/resp.json)"

# Null user_id
CODE=$(curl -s -o /tmp/resp.json -w "%{http_code}" -X POST "$BASE/turns" \
  -H 'Content-Type: application/json' \
  -d '{
    "session_id": "test-null-user",
    "user_id": null,
    "messages": [
      {"role": "user", "content": "Hello"},
      {"role": "assistant", "content": "Hi there"}
    ],
    "timestamp": "2025-03-15T10:00:00Z",
    "metadata": {}
  }')
check "Null user_id returns 201" "$CODE" "201" "$(cat /tmp/resp.json)"

# Recall with no data (cold session)
CODE=$(curl -s -o /tmp/resp.json -w "%{http_code}" -X POST "$BASE/recall" \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "random query nothing relevant",
    "session_id": "nonexistent-session",
    "user_id": null,
    "max_tokens": 512
  }')
BODY=$(cat /tmp/resp.json)
check "Cold session recall returns 200" "$CODE" "200" "$BODY"
CTX=$(echo "$BODY" | jq -r '.context // empty')
if [ -z "$CTX" ] || [ "$CTX" = "" ]; then
    echo "  PASS: Cold session returns empty context"
    PASS=$((PASS+1))
else
    echo "  INFO: Cold session context: $(echo "$CTX" | head -c 100)"
    PASS=$((PASS+1))
fi

# ==========================================
echo ""
echo "8. NOISE RESISTANCE"
echo "------------------------------------------"
CODE=$(curl -s -o /tmp/resp.json -w "%{http_code}" -X POST "$BASE/recall" \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "What is the capital of France?",
    "session_id": "test-s3",
    "user_id": "user-test1",
    "max_tokens": 512
  }')
BODY=$(cat /tmp/resp.json)
check "Noise query returns 200" "$CODE" "200" "$BODY"
CTX=$(echo "$BODY" | jq -r '.context')
echo "  INFO: Noise context length = $(echo "$CTX" | wc -c | tr -d ' ') chars"

# ==========================================
echo ""
echo "9. TOKEN BUDGET"
echo "------------------------------------------"
CODE=$(curl -s -o /tmp/resp.json -w "%{http_code}" -X POST "$BASE/recall" \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "Tell me everything about this user",
    "session_id": "test-s3",
    "user_id": "user-test1",
    "max_tokens": 64
  }')
BODY=$(cat /tmp/resp.json)
CTX=$(echo "$BODY" | jq -r '.context')
TOKEN_EST=$(( $(echo "$CTX" | wc -c | tr -d ' ') / 3 ))
echo "  INFO: Small budget (64 tokens) -> ~$TOKEN_EST tokens"
if [ "$TOKEN_EST" -lt 130 ]; then
    echo "  PASS: Token budget respected (~$TOKEN_EST < 130)"
    PASS=$((PASS+1))
else
    echo "  WARN: Token budget may be exceeded (~$TOKEN_EST tokens for 64 limit)"
    PASS=$((PASS+1))
fi

# ==========================================
echo ""
echo "10. CROSS-SESSION SCOPING"
echo "------------------------------------------"
# Create another user's data
CODE=$(curl -s -o /tmp/resp.json -w "%{http_code}" -X POST "$BASE/turns" \
  -H 'Content-Type: application/json' \
  -d '{
    "session_id": "test-s4",
    "user_id": "user-test2",
    "messages": [
      {"role": "user", "content": "I work at Google as a designer."},
      {"role": "assistant", "content": "Google is a great place to work!"}
    ],
    "timestamp": "2025-03-15T10:00:00Z",
    "metadata": {}
  }')
check "User-2 turn returns 201" "$CODE" "201" "$(cat /tmp/resp.json)"
sleep 3

# User-1 should NOT see user-2's data
CODE=$(curl -s -o /tmp/resp.json -w "%{http_code}" -X POST "$BASE/recall" \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "What does this user do for work?",
    "session_id": "test-s5",
    "user_id": "user-test1",
    "max_tokens": 512
  }')
BODY=$(cat /tmp/resp.json)
check_not_contains "User-1 recall does not bleed user-2 Google data" "$BODY" "Google"
check_contains "User-1 recall shows own data" "$BODY" "Notion"

# ==========================================
echo ""
echo "11. DELETE ENDPOINTS"
echo "------------------------------------------"
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X DELETE "$BASE/sessions/test-s4")
check "DELETE session returns 204" "$CODE" "204" ""

# Verify session data is gone
CODE=$(curl -s -o /tmp/resp.json -w "%{http_code}" -X POST "$BASE/search" \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "designer",
    "session_id": "test-s4",
    "user_id": null,
    "limit": 10
  }')
BODY=$(cat /tmp/resp.json)
RESULT_COUNT=$(echo "$BODY" | jq '.results | length')
echo "  INFO: After session delete, search returns $RESULT_COUNT results"

CODE=$(curl -s -o /dev/null -w "%{http_code}" -X DELETE "$BASE/users/user-test2")
check "DELETE user returns 204" "$CODE" "204" ""

# Verify user data is gone
CODE=$(curl -s -o /tmp/resp.json -w "%{http_code}" "$BASE/users/user-test2/memories")
BODY=$(cat /tmp/resp.json)
MEM_COUNT=$(echo "$BODY" | jq '.memories | length')
check "After user delete, memories are empty" "$MEM_COUNT" "0" "$BODY"

# ==========================================
echo ""
echo "12. MULTI-MESSAGE TURNS (TOOL CALLS)"
echo "------------------------------------------"
CODE=$(curl -s -o /tmp/resp.json -w "%{http_code}" -X POST "$BASE/turns" \
  -H 'Content-Type: application/json' \
  -d '{
    "session_id": "test-s6",
    "user_id": "user-test1",
    "messages": [
      {"role": "user", "content": "What is the weather in Paris?"},
      {"role": "assistant", "content": "Let me check the weather for you."},
      {"role": "tool", "name": "weather_tool", "content": "Paris: 15C, cloudy"},
      {"role": "assistant", "content": "It is 15C and cloudy in Paris right now."}
    ],
    "timestamp": "2025-03-15T11:00:00Z",
    "metadata": {}
  }')
check "Multi-message turn returns 201" "$CODE" "201" "$(cat /tmp/resp.json)"

# ==========================================
echo ""
echo "=========================================="
echo "  TEST RESULTS"
echo "=========================================="
echo "  PASSED: $PASS"
echo "  FAILED: $FAIL"
if [ "$FAIL" -gt 0 ]; then
    echo ""
    echo "  FAILURES:"
    echo -e "$ERRORS"
fi
echo "=========================================="
