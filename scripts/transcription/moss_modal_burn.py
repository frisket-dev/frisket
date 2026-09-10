#!/usr/bin/env python3
"""Run the one-shot MOSS acceptance burn on an ephemeral Modal A10."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import time
from pathlib import Path

import httpx
import modal

from frisket_worker_moss.transcript import parse_moss_transcript

MODEL = "OpenMOSS-Team/MOSS-Transcribe-Diarize"
REVISION = "4a1af868018e7974197f4f018730758012b28c27"
CONTRACT = "frisket.transcription.v1"


def _write(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _copy(sandbox: modal.Sandbox, remote: str, local: Path) -> None:
    try:
        sandbox.filesystem.copy_to_local(remote, local)
        local.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass


def _wait_loaded(
    client: httpx.Client, url: str, token: str, runtime_image_id: str
) -> dict:
    deadline, last = time.monotonic() + 900, "no response"
    while time.monotonic() < deadline:
        try:
            response = client.get(
                f"{url}/v1/capabilities",
                headers={"Authorization": f"Bearer {token}"},
            )
            payload = response.json()
            probe = payload.get("probe", {})
            if response.status_code == 200 and probe.get("available") is True:
                descriptor = payload.get("descriptor", {})
                expected = {
                    "engine": "moss",
                    "model_ids": [MODEL],
                    "revision": REVISION,
                    "runtime_image_id": runtime_image_id,
                }
                if {key: descriptor.get(key) for key in expected} != expected:
                    raise RuntimeError(
                        "worker provenance does not match the burn image"
                    )
                if probe.get("loaded") is not True:
                    raise RuntimeError("available MOSS worker is not loaded")
                return payload
            last = str(probe.get("error") or response.status_code)
        except (httpx.HTTPError, ValueError) as exc:
            last = type(exc).__name__
        time.sleep(2)
    raise RuntimeError(f"MOSS startup timed out: {last}")


def burn(
    image_ref: str,
    audio: Path,
    reference: Path,
    artifacts: Path,
    registry_username: str | None,
    registry_password_env: str,
    registry_secret_name: str | None = None,
) -> dict:
    match = re.fullmatch(r".+@(sha256:[0-9a-f]{64})", image_ref)
    if not match or not audio.is_file() or not reference.is_file():
        raise ValueError("an immutable image ref, audio, and reference are required")
    digest = match.group(1)
    runtime_image_id = f"oci:{digest}"
    artifacts.mkdir(mode=0o700, parents=True, exist_ok=False)

    if registry_secret_name and registry_username:
        raise ValueError(
            "registry secret name and inline registry credentials are mutually exclusive"
        )
    secret = (
        modal.Secret.from_name(registry_secret_name) if registry_secret_name else None
    )
    if registry_username:
        password = os.environ.get(registry_password_env)
        if not password:
            raise ValueError(f"{registry_password_env} is required")
        secret = modal.Secret.from_dict(
            {"REGISTRY_USERNAME": registry_username, "REGISTRY_PASSWORD": password}
        )
    image = modal.Image.from_registry(image_ref, secret=secret)
    app = modal.App("frisket-moss-gpu-burn")
    worker_token = secrets.token_urlsafe(32)
    sandbox = None
    worker_bytes = native_bytes = None
    worker_status = 0
    gpu_evidence: dict[str, str | float] = {}
    startup_seconds = inference_seconds = 0.0

    with modal.enable_output(), app.run():
        started = time.monotonic()
        try:
            sandbox = modal.Sandbox.create(
                app=app,
                image=image,
                gpu="A10",
                cpu=2.0,
                memory=8192,
                timeout=1800,
                encrypted_ports=[9000],
                env={
                    "FRISKET_MOSS_WORKER_TOKEN": worker_token,
                    "FRISKET_MOSS_WORKER_CONCURRENCY": "1",
                    "FRISKET_MOSS_TIMEOUT_SECONDS": "900",
                    "FRISKET_TRANSCRIPTION_WORKER_IMAGE_ID": runtime_image_id,
                    "HF_HUB_OFFLINE": "1",
                    "TRANSFORMERS_OFFLINE": "1",
                },
            )
            sandbox.filesystem.copy_from_local(audio, "/tmp/burn.wav")
            _gpu_logger = sandbox.exec(
                "sh",
                "-c",
                "nvidia-smi --query-gpu=timestamp,name,uuid,driver_version,memory.total,memory.used,utilization.gpu --format=csv,noheader,nounits -l 1 > /tmp/gpu.csv",
                timeout=1800,
            )
            tunnel = sandbox.tunnels(timeout=300).get(9000)
            if tunnel is None:
                raise RuntimeError("Modal did not expose worker port 9000")

            with httpx.Client(
                timeout=httpx.Timeout(900, connect=30), trust_env=False
            ) as client:
                capabilities = _wait_loaded(
                    client, tunnel.url, worker_token, runtime_image_id
                )
                startup_seconds = time.monotonic() - started
                runtime = sandbox.exec(
                    "sh",
                    "-c",
                    "for f in /proc/[0-9]*/cmdline; do tr '\\000' ' ' < \"$f\" 2>/dev/null; echo; done",
                )
                if runtime.wait():
                    raise RuntimeError("could not inspect the MOSS runtime process")
                runtime_text = runtime.stdout.read()
                if "--dtype bfloat16" not in runtime_text:
                    raise RuntimeError("MOSS runtime is not configured for bfloat16")
                _write(artifacts / "runtime-processes.log", runtime_text.encode())
                inference_started = time.monotonic()
                with audio.open("rb") as handle:
                    response = client.post(
                        f"{tunnel.url}/v1/transcribe",
                        headers={"Authorization": f"Bearer {worker_token}"},
                        data={
                            "contract_version": CONTRACT,
                            "engine": "moss",
                            "options": "{}",
                        },
                        files={"file": (audio.name, handle, "audio/wav")},
                    )
                inference_seconds = time.monotonic() - inference_started
                worker_bytes = response.content
                worker_status = response.status_code

            _write(artifacts / "worker-response.json", worker_bytes)
            _write(
                artifacts / "capabilities.json",
                (json.dumps(capabilities, indent=2, sort_keys=True) + "\n").encode(),
            )

            process = sandbox.exec(
                "curl",
                "--fail-with-body",
                "--silent",
                "--show-error",
                "--output",
                "/tmp/native.json",
                "--form",
                "file=@/tmp/burn.wav",
                "--form",
                f"model={MODEL}",
                "--form",
                "response_format=json",
                "--form",
                "temperature=0",
                "--form",
                "max_completion_tokens=65536",
                "http://127.0.0.1:8000/v1/audio/transcriptions",
                timeout=900,
            )
            if process.wait():
                raise RuntimeError(f"native request failed: {process.stderr.read()}")
            sandbox.filesystem.copy_to_local(
                "/tmp/native.json", artifacts / "native-response.json"
            )
            native_bytes = (artifacts / "native-response.json").read_bytes()
            (artifacts / "native-response.json").chmod(stat.S_IRUSR | stat.S_IWUSR)
            native_text = json.loads(native_bytes).get("text")
            if not isinstance(native_text, str) or not parse_moss_transcript(
                native_text
            ):
                raise RuntimeError("real native MOSS output did not parse")

            gpu_path = artifacts / "gpu.csv"
            sandbox.filesystem.copy_to_local("/tmp/gpu.csv", gpu_path)
            gpu_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            rows = [row.split(",") for row in gpu_path.read_text().splitlines()]
            try:
                peak_vram = max(float(row[5].strip()) for row in rows)
                gpu_name = rows[-1][1].strip()
            except (IndexError, ValueError) as exc:
                raise RuntimeError("GPU sampler produced invalid evidence") from exc
            if "A10" not in gpu_name or peak_vram <= 0:
                raise RuntimeError("GPU sampler did not prove an active Modal A10")
            gpu_evidence = {"name": gpu_name, "peak_vram_mib": peak_vram}
            if worker_status >= 400:
                raise RuntimeError(
                    f"worker returned HTTP {worker_status}; raw evidence was preserved"
                )
        finally:
            if sandbox:
                _copy(sandbox, "/tmp/gpu.csv", artifacts / "gpu.csv")
                sandbox.terminate(wait=True)
                _write(artifacts / "worker-stdout.log", sandbox.stdout.read().encode())
                _write(artifacts / "worker-stderr.log", sandbox.stderr.read().encode())
                sandbox.detach()

    if worker_bytes is None or native_bytes is None:
        raise RuntimeError("burn did not produce both worker and native responses")
    evaluator = (
        Path(__file__).resolve().parents[2]
        / "sidecar/scripts/transcription_fixture_eval.py"
    )
    evaluated = subprocess.run(
        [
            sys.executable,
            str(evaluator),
            "--reference",
            str(reference),
            "--response",
            str(artifacts / "worker-response.json"),
            "--audio",
            str(audio),
        ],
        capture_output=True,
        check=False,
    )
    if evaluated.stdout:
        _write(artifacts / "evaluation.json", evaluated.stdout)
    if evaluated.returncode:
        raise RuntimeError(
            "fixture evaluator rejected the burn: "
            + evaluated.stderr.decode(errors="replace").strip()
        )
    summary = {
        "schema": "frisket.transcription.moss-modal-burn.v1",
        "image": image_ref,
        "gpu": gpu_evidence,
        "model_id": MODEL,
        "model_revision": REVISION,
        "startup_seconds": round(startup_seconds, 3),
        "worker_inference_seconds": round(inference_seconds, 3),
        "worker_response_sha256": hashlib.sha256(worker_bytes).hexdigest(),
        "native_response_sha256": hashlib.sha256(native_bytes).hexdigest(),
        "evaluation_sha256": hashlib.sha256(evaluated.stdout).hexdigest(),
    }
    _write(
        artifacts / "burn-summary.json",
        (json.dumps(summary, indent=2, sort_keys=True) + "\n").encode(),
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--registry-username")
    parser.add_argument("--registry-secret-name")
    parser.add_argument("--registry-password-env", default="FRISKET_GHCR_READ_TOKEN")
    args = parser.parse_args()
    try:
        result = burn(
            args.image,
            args.audio,
            args.reference,
            args.artifacts,
            args.registry_username,
            args.registry_password_env,
            args.registry_secret_name,
        )
    except (ValueError, RuntimeError, OSError, httpx.HTTPError) as exc:
        parser.exit(2, f"burn failed: {exc}\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
