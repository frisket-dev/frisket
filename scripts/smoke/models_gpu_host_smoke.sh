#!/usr/bin/env bash
# Live edge/auth/isolation smoke for docker-compose.models-gpu.yml.
set -euo pipefail

: "${MODELS_GPU_ORIGIN:?set MODELS_GPU_ORIGIN, e.g. https://models-gpu.example.com}"
: "${MODELS_GPU_TOKEN:?set MODELS_GPU_TOKEN to the Boundary-A bearer}"
: "${SMOKE_EXPECT_ENGINE:?set SMOKE_EXPECT_ENGINE to the active engine name}"
: "${SMOKE_EXPECT_REVISION:?set SMOKE_EXPECT_REVISION to its pinned model revision}"
: "${SMOKE_EXPECT_RUNTIME_IMAGE_ID:?set SMOKE_EXPECT_RUNTIME_IMAGE_ID to its oci:sha256:... or modal:im-... identity}"

command -v python3 >/dev/null 2>&1 || {
  echo "models GPU smoke requires python3 for strict contract validation" >&2
  exit 2
}

SMOKE_TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$SMOKE_TMP_DIR"' EXIT

MODELS_GPU_ORIGIN="${MODELS_GPU_ORIGIN%/}"
SMOKE_TIMEOUT="${SMOKE_TIMEOUT:-15}"
SMOKE_WRONG_TOKEN="${SMOKE_WRONG_TOKEN:-definitely-not-the-models-token}"
CURL_OPTS=(-sS --max-time "$SMOKE_TIMEOUT")
if [ "${SMOKE_INSECURE:-0}" = "1" ]; then
  CURL_OPTS+=(-k)
fi

PASS=0
FAIL=0
FAILURES=()

request_status() {
  local method="$1" origin="$2" path="$3" token="${4:-}"
  local -a headers=()
  if [ -n "$token" ]; then
    headers+=(-H "Authorization: Bearer $token")
  fi
  curl "${CURL_OPTS[@]}" -o /dev/null -w '%{http_code}' \
    -X "$method" "${headers[@]}" "$origin$path" 2>/dev/null || true
}

capabilities_status() {
  curl "${CURL_OPTS[@]}" -o "$SMOKE_TMP_DIR/capabilities.json" -w '%{http_code}' \
    -H "Authorization: Bearer $MODELS_GPU_TOKEN" \
    "$MODELS_GPU_ORIGIN/capabilities" 2>/dev/null || true
}

validate_capabilities() {
  python3 - "$SMOKE_TMP_DIR/capabilities.json" \
    "$SMOKE_EXPECT_ENGINE" "$SMOKE_EXPECT_REVISION" \
    "$SMOKE_EXPECT_RUNTIME_IMAGE_ID" <<'PY'
import json
import sys

path, expected_engine, expected_revision, expected_runtime_image_id = sys.argv[1:]
with open(path, encoding="utf-8") as handle:
    payload = json.load(handle)
matches = [
    engine
    for engine in payload.get("engines", [])
    if isinstance(engine, dict) and engine.get("name") == expected_engine
]
if len(matches) != 1:
    raise SystemExit(f"expected exactly one {expected_engine!r} capability")
engine = matches[0]
if engine.get("available") is not True or engine.get("error") is not None:
    raise SystemExit("expected engine is not truthfully available")
if engine.get("revision") != expected_revision:
    raise SystemExit("engine revision does not match expectation")
if engine.get("runtime_image_id") != expected_runtime_image_id:
    raise SystemExit("engine runtime image id does not match expectation")
if not isinstance(engine.get("loaded"), bool):
    raise SystemExit("engine loaded state is not boolean")
if "frisket.transcription.v1" not in engine.get("contract_versions", []):
    raise SystemExit("engine does not advertise transcription contract v1")
PY
}

malformed_transcribe_status() {
  local token="$1"
  local -a headers=()
  if [ -n "$token" ]; then
    headers+=(-H "Authorization: Bearer $token")
  fi
  # Multipart on purpose, but missing the required `files` part. A 400 proves
  # the request reached the versioned gateway without running inference.
  curl "${CURL_OPTS[@]}" -o /dev/null -w '%{http_code}' \
    "${headers[@]}" \
    -F 'contract_version=frisket.transcription.v1' \
    -F 'engine=contract-stub' \
    -F 'options={}' \
    "$MODELS_GPU_ORIGIN/v1/transcribe" 2>/dev/null || true
}

stub_transcribe_status() {
  # /dev/null is sufficient: the stub verifies a real spooled file exists but
  # deliberately performs no decoding or model inference.
  curl "${CURL_OPTS[@]}" -o "$SMOKE_TMP_DIR/stub-response.json" -w '%{http_code}' \
    -H "Authorization: Bearer $MODELS_GPU_TOKEN" \
    -F 'contract_version=frisket.transcription.v1' \
    -F 'engine=contract-stub' \
    -F 'options={"context":"Frisket","language":"en"}' \
    -F 'files=@/dev/null;filename=stub.wav;type=audio/wav' \
    "$MODELS_GPU_ORIGIN/v1/transcribe" 2>/dev/null || true
}

validate_stub_response() {
  python3 - "$SMOKE_TMP_DIR/stub-response.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
if payload.get("contract_version") != "frisket.transcription.v1":
    raise SystemExit("stub response has the wrong contract version")
results = payload.get("results")
if not isinstance(results, list) or len(results) != 1:
    raise SystemExit("stub response must contain exactly one result")
result = results[0]
expected_options = {"context": "Frisket", "language": "en"}
if result.get("engine") != "contract-stub":
    raise SystemExit("stub response has the wrong engine")
if result.get("model_ids") != ["frisket/contract-stub"]:
    raise SystemExit("stub response has the wrong model provenance")
if result.get("revision") != "transcription-contract-v1":
    raise SystemExit("stub response has the wrong revision")
if result.get("accepted_options") != expected_options:
    raise SystemExit("stub response did not preserve accepted options")
segments = result.get("segments")
if not isinstance(segments, list) or len(segments) != 1:
    raise SystemExit("stub response must contain exactly one segment")
segment = segments[0]
if segment.get("speaker") != "SPEAKER_00":
    raise SystemExit("stub response did not preserve speaker")
if segment.get("speaker_confidence") != "diagnostic":
    raise SystemExit("stub response did not preserve speaker confidence")
if segment.get("words") != [{"word": "contract", "start": 0.0, "end": 0.25}]:
    raise SystemExit("stub response did not preserve word timings")
timings = result.get("timings", {})
if not {"worker.adapter_load_seconds", "worker.inference_seconds"} <= set(timings):
    raise SystemExit("stub response is missing worker timing metrics")
PY
}

expect_status() {
  local description="$1" actual="$2" expected="$3"
  if [ "$actual" = "$expected" ]; then
    PASS=$((PASS + 1))
    echo "[PASS] $description -> $actual"
  else
    FAIL=$((FAIL + 1))
    FAILURES+=("$description -> ${actual:-<no response>} (expected $expected)")
    echo "[FAIL] $description -> ${actual:-<no response>} (expected $expected)"
  fi
}

echo "=== Public models-only edge ==="
expect_status "/health is unauthenticated liveness" \
  "$(request_status GET "$MODELS_GPU_ORIGIN" /health)" 200
capabilities_http_status="$(capabilities_status)"
expect_status "/capabilities accepts the models token" \
  "$capabilities_http_status" 200
if [ "$capabilities_http_status" = "200" ]; then
  if validate_capabilities; then
    PASS=$((PASS + 1))
    echo "[PASS] /capabilities reports expected available engine provenance"
  else
    FAIL=$((FAIL + 1))
    FAILURES+=("/capabilities engine availability/provenance validation failed")
    echo "[FAIL] /capabilities engine availability/provenance validation failed"
  fi
fi
expect_status "/capabilities rejects a missing token" \
  "$(request_status GET "$MODELS_GPU_ORIGIN" /capabilities)" 401
expect_status "/capabilities rejects a wrong token" \
  "$(request_status GET "$MODELS_GPU_ORIGIN" /capabilities "$SMOKE_WRONG_TOKEN")" 403
expect_status "authenticated malformed v1 request reaches gateway validation" \
  "$(malformed_transcribe_status "$MODELS_GPU_TOKEN")" 400
expect_status "wrong token is rejected before v1 request parsing" \
  "$(malformed_transcribe_status "$SMOKE_WRONG_TOKEN")" 403
if [ "${SMOKE_EXPECT_STUB:-0}" = "1" ]; then
  stub_http_status="$(stub_transcribe_status)"
  expect_status "contract stub proves gateway and worker tokens match" \
    "$stub_http_status" 200
  if [ "$stub_http_status" = "200" ]; then
    if validate_stub_response; then
      PASS=$((PASS + 1))
      echo "[PASS] contract stub preserves the v1 result envelope"
    else
      FAIL=$((FAIL + 1))
      FAILURES+=("contract stub v1 result-envelope validation failed")
      echo "[FAIL] contract stub v1 result-envelope validation failed"
    fi
  fi
fi

check_raw_origin_unreachable() {
  local label="$1" origin="$2" path="$3"
  if [ -z "$origin" ]; then
    echo "[SKIP] $label raw-origin isolation (origin not provided)"
    return
  fi
  local status
  status="$(request_status GET "${origin%/}" "$path")"
  if [ -z "$status" ] || [ "$status" = "000" ]; then
    PASS=$((PASS + 1))
    echo "[PASS] $label raw origin is unreachable from this vantage point"
  else
    FAIL=$((FAIL + 1))
    FAILURES+=("$label raw origin responded with HTTP $status")
    echo "[FAIL] $label raw origin responded with HTTP $status"
  fi
}

echo ""
echo "=== Optional raw-port bypass probes ==="
check_raw_origin_unreachable "gateway" "${GATEWAY_RAW_ORIGIN:-}" /health
check_raw_origin_unreachable "worker" "${WORKER_RAW_ORIGIN:-}" /health

echo ""
echo "=== Summary: $PASS passed, $FAIL failed ==="
if [ "$FAIL" -gt 0 ]; then
  printf '  - %s\n' "${FAILURES[@]}"
  exit 1
fi
