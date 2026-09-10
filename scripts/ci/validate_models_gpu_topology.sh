#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
compose_file="$repo_root/docker-compose.models-gpu.yml"
caddyfile="$repo_root/deploy/Caddyfile.models-gpu"
compose_config="$(mktemp)"
caddy_config="$(mktemp)"
trap 'rm -f -- "$compose_config" "$caddy_config"' EXIT

export FRISKET_MODELS_TOKEN=test-boundary-a-token
export FRISKET_MODELS_HOST=models.example.test
export FRISKET_MODELS_MAX_REQUEST_BODY=270MB

docker compose -f "$compose_file" --profile gpu config --format json \
  >"$compose_config"

# Assert against Compose's rendered service graph, including interpolation and
# profile expansion, rather than re-parsing the YAML in a unit test.
jq -e '
  .services as $services |
  ($services | keys | sort) == ["caddy", "gateway", "gpu-lease-init", "gpu-worker"] and
  ([$services | to_entries[] | select(((.value.ports // []) | length) > 0) |
    .key] | sort) == ["caddy"] and
  ($services.caddy.ports == [{"mode": "ingress", "target": 443,
    "published": "443", "protocol": "tcp"}]) and
  (($services.caddy.networks | keys) == ["edge"]) and
  (($services["gpu-worker"].networks | keys) == ["workers"]) and
  (($services.gateway.networks | keys | sort) == ["edge", "workers"]) and
  ((.networks.workers.internal // false) == false) and
  $services["gpu-worker"] as $worker |
  ($worker.profiles == ["gpu"]) and
  ($worker.entrypoint == ["python3", "-m",
    "frisket_models.transcription.gpu_lease"]) and
  ($worker.depends_on["gpu-lease-init"].condition ==
    "service_completed_successfully") and
  ($worker.environment.FRISKET_TRANSCRIPTION_WORKER_CONCURRENCY == "1") and
  ($worker.environment.FRISKET_WHISPER_TURBO_LOCAL_FILES_ONLY == "true") and
  ($worker.environment.FRISKET_VIBEVOICE_ASR_LOCAL_FILES_ONLY == "true") and
  ($worker.environment.FRISKET_GPU_WORKER_REQUIRE_TOKEN == "1") and
  ($worker.environment.FRISKET_GPU_LEASE_FILE == "/run/frisket-gpu/gpu-worker.lock") and
  ([$worker.volumes[] | select(.source == "gpu-lease" and
    .target == "/run/frisket-gpu")] | length) == 1 and
  $services["gpu-lease-init"] as $init |
  ($init.profiles == ["gpu"]) and
  ($init.user == "0:0") and
  ($init.entrypoint[0:2] == ["python3", "-c"]) and
  ([$init.volumes[] | select(.source == "gpu-lease" and
    .target == "/run/frisket-gpu")] | length) == 1 and
  (.volumes["gpu-lease"].name == "frisket-gpu-worker-lease") and
  ($worker.deploy.resources.reservations.devices == [{"capabilities": ["gpu"],
    "driver": "nvidia", "count": 1}])
' "$compose_config" >/dev/null

caddy_image="$(jq -er '.services.caddy.image' "$compose_config")"
gateway_max_bytes="$(
  jq -er '.services.gateway.environment.FRISKET_TRANSCRIPTION_MAX_UPLOAD_BYTES | tonumber' \
    "$compose_config"
)"
caddy_args=(--rm --network none
  -e "FRISKET_MODELS_HOST=$FRISKET_MODELS_HOST"
  -e "FRISKET_MODELS_MAX_REQUEST_BODY=$FRISKET_MODELS_MAX_REQUEST_BODY"
  -v "$caddyfile:/etc/caddy/Caddyfile:ro"
  "$caddy_image")

docker run "${caddy_args[@]}" \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker run "${caddy_args[@]}" \
  caddy adapt --config /etc/caddy/Caddyfile --adapter caddyfile \
  >"$caddy_config"

# Caddy's adapted runtime graph proves both routing and handler order: the body
# limit runs before the sole proxy and that proxy can reach only the gateway.
jq -e --argjson gateway_max_bytes "$gateway_max_bytes" '
  [.. | objects | select(.handler? == "reverse_proxy")] as $proxies |
  [.. | objects | select(.handler? == "request_body")] as $body_limits |
  ($proxies == [{"handler": "reverse_proxy",
    "upstreams": [{"dial": "gateway:8500"}]}]) and
  (($body_limits | length) == 1) and
  ($body_limits[0].max_size > $gateway_max_bytes) and
  ([
    .. | objects | select(has("handle")) | .handle | select(type == "array") |
    map(.handler // "") | select(index("request_body") != null and
      index("reverse_proxy") != null and
      index("request_body") < index("reverse_proxy")
    )
  ] | length) == 1
' "$caddy_config" >/dev/null

echo "models GPU topology is valid"
