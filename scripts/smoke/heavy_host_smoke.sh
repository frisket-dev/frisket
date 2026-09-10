#!/usr/bin/env bash
# scripts/smoke/heavy_host_smoke.sh — live smoke matrix for the split-host GPU
# bundle's Caddy front door. Exercises every
# allowlisted route x {correct, wrong, missing token}, the always-403
# management routes, and (optionally) the unenforced-edge bypass check.
#
# No docker assumptions: this hits plain HTTP(S) origins over the network,
# so it runs the same way against a local `docker compose -f
# docker-compose.heavy.yml up` stack or a real deployed heavy host.
#
# Required env:
#   LLM_ORIGIN               e.g. https://llm.heavy.example.com
#   MODELS_ORIGIN             e.g. https://models.heavy.example.com
#   LLM_TOKEN                 the inference bearer token
#   LLM_PROVISIONING_TOKEN    the provisioning bearer token
#   MODELS_TOKEN              the frisket-models sidecar bearer token
#
# Optional env:
#   OLLAMA_RAW_ORIGIN   The Ollama daemon's OWN origin (e.g.
#                       http://10.0.0.5:11434), reachable ONLY if your
#                       network isolation is broken — the daemon must never
#                       be reachable from outside the heavy host itself. Set
#                       this to actively probe for that bypass. Left unset,
#                       the check is SKIPPED, not assumed safe: an
#                       unreachable/refused connection from where you're
#                       running this script proves nothing about reachability
#                       from other vantage points.
#   SMOKE_MODEL         Model name used in request bodies. Defaults to an
#                       obviously fake name so POST /api/pull fails fast
#                       against a real Ollama instead of downloading real
#                       multi-GB weights, and chat/embeddings return a fast
#                       "model not found" upstream error instead of a real
#                       inference call.
#   SMOKE_INSECURE       Set to 1 to skip TLS verification (curl -k) — needed
#                       when the heavy host uses Caddy's internal/LAN CA
#                       (`tls internal`) and you have not installed its root
#                       certificate locally.
#   SMOKE_TIMEOUT       Per-request curl timeout in seconds (default 15).
#
# Exit code is nonzero if any check fails.
set -euo pipefail

: "${LLM_ORIGIN:?set LLM_ORIGIN, e.g. https://llm.heavy.example.com}"
: "${MODELS_ORIGIN:?set MODELS_ORIGIN, e.g. https://models.heavy.example.com}"
: "${LLM_TOKEN:?set LLM_TOKEN to the inference bearer token}"
: "${LLM_PROVISIONING_TOKEN:?set LLM_PROVISIONING_TOKEN to the provisioning bearer token}"
: "${MODELS_TOKEN:?set MODELS_TOKEN to the frisket-models sidecar bearer token}"

SMOKE_MODEL="${SMOKE_MODEL:-frisket-smoke-test-nonexistent-model}"
SMOKE_TIMEOUT="${SMOKE_TIMEOUT:-15}"
CURL_OPTS=(-s --max-time "$SMOKE_TIMEOUT")
if [ "${SMOKE_INSECURE:-0}" = "1" ]; then
  CURL_OPTS+=(-k)
fi

PASS=0
FAIL=0
FAILURES=()

log() { echo "$*"; }

# request METHOD ORIGIN PATH TOKEN [BODY]
# Prints the numeric HTTP status on stdout; empty string on connect failure.
request() {
  local method="$1" origin="$2" path="$3" token="$4" body="${5:-}"
  local -a hdrs=()
  if [ -n "$token" ]; then
    hdrs+=(-H "Authorization: Bearer $token")
  fi
  local -a data_opt=()
  if [ -n "$body" ]; then
    data_opt=(-X "$method" --data "$body" -H "Content-Type: application/json")
  else
    data_opt=(-X "$method")
  fi
  curl "${CURL_OPTS[@]}" -o /dev/null -w '%{http_code}' \
    "${hdrs[@]}" "${data_opt[@]}" "$origin$path" 2>/dev/null || true
}

# expect_status DESC METHOD ORIGIN PATH TOKEN BODY WANT_CLASS
# WANT_CLASS is "not403" (door let it through — upstream owns the real
# status) or "403" (door must deny).
expect_status() {
  local desc="$1" method="$2" origin="$3" path="$4" token="$5" body="$6" want="$7"
  local status
  status="$(request "$method" "$origin" "$path" "$token" "$body")"
  local ok=0
  case "$want" in
    403)
      [ "$status" = "403" ] && ok=1
      ;;
    not403)
      [ -n "$status" ] && [ "$status" != "403" ] && [ "$status" != "000" ] && ok=1
      ;;
  esac
  if [ "$ok" = "1" ]; then
    PASS=$((PASS + 1))
    log "[PASS] $desc -> status=${status:-<no response>} (want ${want})"
  else
    FAIL=$((FAIL + 1))
    FAILURES+=("$desc -> status=${status:-<no response>} (want ${want})")
    log "[FAIL] $desc -> status=${status:-<no response>} (want ${want})"
  fi
}

CHAT_BODY='{"model":"'"$SMOKE_MODEL"'","messages":[{"role":"user","content":"hi"}],"stream":false}'
EMBED_BODY='{"model":"'"$SMOKE_MODEL"'","input":"hi"}'
PULL_BODY='{"model":"'"$SMOKE_MODEL"'"}'
DELETE_BODY='{"model":"'"$SMOKE_MODEL"'"}'
PUSH_BODY='{"model":"'"$SMOKE_MODEL"'"}'
COPY_BODY='{"source":"'"$SMOKE_MODEL"'","destination":"'"$SMOKE_MODEL"'-copy"}'

log "=== Allowlisted routes: correct / wrong / missing token ==="

# POST /v1/chat/completions — inference token only.
expect_status "POST /v1/chat/completions correct(inference)" POST "$LLM_ORIGIN" /v1/chat/completions "$LLM_TOKEN" "$CHAT_BODY" not403
expect_status "POST /v1/chat/completions wrong(provisioning)" POST "$LLM_ORIGIN" /v1/chat/completions "$LLM_PROVISIONING_TOKEN" "$CHAT_BODY" 403
expect_status "POST /v1/chat/completions missing" POST "$LLM_ORIGIN" /v1/chat/completions "" "$CHAT_BODY" 403

# POST /v1/embeddings — inference token only.
expect_status "POST /v1/embeddings correct(inference)" POST "$LLM_ORIGIN" /v1/embeddings "$LLM_TOKEN" "$EMBED_BODY" not403
expect_status "POST /v1/embeddings wrong(provisioning)" POST "$LLM_ORIGIN" /v1/embeddings "$LLM_PROVISIONING_TOKEN" "$EMBED_BODY" 403
expect_status "POST /v1/embeddings missing" POST "$LLM_ORIGIN" /v1/embeddings "" "$EMBED_BODY" 403

# GET /v1/models — inference token only.
expect_status "GET /v1/models correct(inference)" GET "$LLM_ORIGIN" /v1/models "$LLM_TOKEN" "" not403
expect_status "GET /v1/models wrong(provisioning)" GET "$LLM_ORIGIN" /v1/models "$LLM_PROVISIONING_TOKEN" "" 403
expect_status "GET /v1/models missing" GET "$LLM_ORIGIN" /v1/models "" "" 403

# GET /api/tags — EITHER token is valid.
expect_status "GET /api/tags correct(inference)" GET "$LLM_ORIGIN" /api/tags "$LLM_TOKEN" "" not403
expect_status "GET /api/tags correct(provisioning)" GET "$LLM_ORIGIN" /api/tags "$LLM_PROVISIONING_TOKEN" "" not403
expect_status "GET /api/tags wrong(models token)" GET "$LLM_ORIGIN" /api/tags "$MODELS_TOKEN" "" 403
expect_status "GET /api/tags missing" GET "$LLM_ORIGIN" /api/tags "" "" 403

# POST /api/pull — provisioning token only (SMOKE_MODEL is fake so this
# fails fast against a real Ollama instead of downloading real weights).
expect_status "POST /api/pull correct(provisioning)" POST "$LLM_ORIGIN" /api/pull "$LLM_PROVISIONING_TOKEN" "$PULL_BODY" not403
expect_status "POST /api/pull wrong(inference)" POST "$LLM_ORIGIN" /api/pull "$LLM_TOKEN" "$PULL_BODY" 403
expect_status "POST /api/pull missing" POST "$LLM_ORIGIN" /api/pull "" "$PULL_BODY" 403

log ""
log "=== Always-403 management routes (never allowlisted, any token) ==="

for tok_name in "none:" "inference:$LLM_TOKEN" "provisioning:$LLM_PROVISIONING_TOKEN"; do
  tname="${tok_name%%:*}"
  tval="${tok_name#*:}"
  expect_status "POST /api/delete ($tname token)" POST "$LLM_ORIGIN" /api/delete "$tval" "$DELETE_BODY" 403
  expect_status "POST /api/push ($tname token)" POST "$LLM_ORIGIN" /api/push "$tval" "$PUSH_BODY" 403
  expect_status "POST /api/copy ($tname token)" POST "$LLM_ORIGIN" /api/copy "$tval" "$COPY_BODY" 403
  expect_status "GET /api/blobs/sha256:0000 ($tname token)" GET "$LLM_ORIGIN" "/api/blobs/sha256:0000" "$tval" "" 403
  expect_status "HEAD /api/blobs/sha256:0000 ($tname token)" HEAD "$LLM_ORIGIN" "/api/blobs/sha256:0000" "$tval" "" 403
done

log ""
log "=== models.<host> passthrough (sidecar enforces its own token) ==="

MODELS_STATUS_CORRECT="$(request GET "$MODELS_ORIGIN" /healthz "$MODELS_TOKEN" "")"
log "[INFO] GET /healthz with sidecar token -> status=${MODELS_STATUS_CORRECT:-<no response>} (informational: exact route/status depends on the sidecar's own API, not this door)"

log ""
log "=== Unenforced-edge bypass check ==="
if [ -n "${OLLAMA_RAW_ORIGIN:-}" ]; then
  RAW_STATUS="$(request GET "$OLLAMA_RAW_ORIGIN" /api/tags "" "")"
  if [ -n "$RAW_STATUS" ] && [ "$RAW_STATUS" != "000" ] && [ "$RAW_STATUS" -lt 400 ] 2>/dev/null; then
    FAIL=$((FAIL + 1))
    FAILURES+=("OLLAMA_RAW_ORIGIN tokenless /api/tags succeeded (status=$RAW_STATUS)")
    log "[FAIL] tokenless GET \$OLLAMA_RAW_ORIGIN/api/tags -> status=$RAW_STATUS"
    log "       WARNING: Ollama's daemon is reachable directly, bypassing the Caddy"
    log "       front door entirely. This means network isolation between the"
    log "       heavy host's internal services and the outside world is NOT in"
    log "       place — anyone who can reach this origin gets full unauthenticated"
    log "       access to Ollama's management API (pull/delete/push/copy/blobs),"
    log "       no matter how tight the Caddy allowlist above is. Fix the"
    log "       firewall/VPN/network boundary so ollama's port is reachable only"
    log "       from the caddy container (docker-compose.heavy.yml uses \`expose\`,"
    log "       never \`ports\`, for exactly this reason — this failure means"
    log "       something outside that compose file is punching a hole through)."
  else
    PASS=$((PASS + 1))
    log "[PASS] tokenless GET \$OLLAMA_RAW_ORIGIN/api/tags did not succeed (status=${RAW_STATUS:-<no response>}) — the raw daemon is not reachable from here, as expected"
  fi
else
  log "[SKIP] OLLAMA_RAW_ORIGIN not set — bypass check skipped. This is NOT the"
  log "       same as confirming isolation is safe: it only means this script"
  log "       didn't check. Set OLLAMA_RAW_ORIGIN to the Ollama daemon's own"
  log "       address (reachable only if isolation is broken) to actively test."
fi

log ""
log "=== Summary: ${PASS} passed, ${FAIL} failed ==="
if [ "$FAIL" -gt 0 ]; then
  log "Failures:"
  for f in "${FAILURES[@]}"; do
    log "  - $f"
  done
  exit 1
fi
exit 0
