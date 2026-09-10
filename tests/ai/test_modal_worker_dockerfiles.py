from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_locked_modal_workers_ignore_ambient_package_mirrors() -> None:
    sync = "uv sync --locked --no-dev --default-index https://pypi.org/simple"
    for worker in ("whisper_turbo", "parakeet_tdt"):
        dockerfile = ROOT / "sidecar" / "workers" / worker / "Dockerfile"
        # rule19: two-sources — both independently maintained worker images must pin the same install command
        assert sync in dockerfile.read_text(encoding="utf-8")
