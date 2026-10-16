#!/usr/bin/env python3
"""Run one isolated video retrieval arm through production producer/runtime code."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import resource
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np

from frisket.engine._workers.embeddinggemma_worker import EmbeddingGemma2Runtime
from frisket.engine._workers.embeddinggemma_session import EmbeddingGemma2Session
from frisket.engine.executor.action_inventory import BoundLocalFile
from frisket.retrieval.encoding import EmbeddingGemma2Space, EncodingPurpose
from frisket.retrieval.media import (
    AudioWindow,
    AudioWindowRecipe,
    FixedClipRecipe,
    LocatedFrame,
    LocatedVideoClip,
    PeriodicFrameRecipe,
    ShotClipRecipe,
    produce_media,
)


ARMS = (
    "probe5",
    "frames5",
    "fixed5",
    "fixed10",
    "shot5",
    "shot10",
    "audio5",
    "transcript",
    "order5",
    "timing",
    "session_probe5",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", choices=ARMS, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--fixture-root", type=Path, required=True)
    parser.add_argument("--model-path", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--producer-sha", required=True)
    parser.add_argument("--runtime-sha", required=True)
    parser.add_argument("--runtime-python", type=Path)
    parser.add_argument("--app-code-root", type=Path)
    return parser.parse_args()


def load_corpus(path: Path) -> dict[str, Any]:
    corpus = json.loads(path.read_text(encoding="utf-8"))
    if corpus.get("schema_version") != "video_retrieval_corpus.v1":
        raise ValueError("unsupported corpus schema")
    return corpus


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


async def produce(path: Path, recipe: Any) -> Any:
    digest = sha256(path)
    with path.open("rb") as stream:
        return await produce_media(
            BoundLocalFile(stream=stream, sha256=f"sha256:{digest}"), recipe
        )


def location(item: Any, source_id: str) -> dict[str, Any]:
    if isinstance(item, LocatedFrame):
        start = end = item.point.seconds
    elif isinstance(item, (LocatedVideoClip, AudioWindow)):
        start, end = item.start_seconds, item.end_seconds
    else:
        raise TypeError(f"unsupported located item: {type(item).__name__}")
    return {"source_id": source_id, "start": start, "end": end}


def stage_item(item: Any, root: Path, index: int) -> tuple[dict[str, Any], int]:
    if isinstance(item, LocatedFrame):
        path = root / f"image-{index:04d}.jpg"
        path.write_bytes(item.input.image_bytes)
        return {"kind": "image", "path": str(path)}, path.stat().st_size
    if isinstance(item, LocatedVideoClip):
        frame_paths = []
        total = 0
        for frame_index, frame in enumerate(item.input.frames):
            path = root / f"clip-{index:04d}-frame-{frame_index:03d}.jpg"
            path.write_bytes(frame.image_bytes)
            frame_paths.append(str(path))
            total += path.stat().st_size
        return {"kind": "video", "frames": frame_paths}, total
    if isinstance(item, AudioWindow):
        path = root / f"audio-{index:04d}.pcm"
        path.write_bytes(item.input.pcm16le_bytes)
        return {
            "kind": "audio",
            "path": str(path),
            "sample_rate": item.input.sample_rate,
        }, path.stat().st_size
    raise TypeError(f"unsupported located item: {type(item).__name__}")


def rank(
    query_vector: list[float],
    item_vectors: list[list[float]],
    item_locations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    query = np.asarray(query_vector, dtype=np.float32)
    items = np.asarray(item_vectors, dtype=np.float32)
    scores = items @ query
    order = np.argsort(-scores, kind="stable")
    return [
        {
            "item_id": f"item-{int(index):04d}",
            **item_locations[int(index)],
            "score": float(scores[int(index)]),
        }
        for index in order
    ]


def peak_rss_kib() -> int:
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


def arm_spec(arm: str) -> tuple[str, Any] | None:
    if arm == "probe5":
        return "visual", FixedClipRecipe(
            duration_seconds=5.0,
            stride_seconds=5.0,
            sample_fps=1.0,
            max_clips=1,
            max_dimension=448,
        )
    if arm == "frames5":
        return "visual", PeriodicFrameRecipe(
            interval_seconds=5.0,
            adjacent_dedup_threshold=0.01,
            max_frames=20,
            max_dimension=448,
        )
    if arm == "fixed5":
        return "visual", FixedClipRecipe(
            duration_seconds=5.0,
            stride_seconds=4.0,
            sample_fps=1.0,
            max_clips=20,
            max_dimension=448,
        )
    if arm == "fixed10":
        return "visual", FixedClipRecipe(
            duration_seconds=10.0,
            stride_seconds=8.0,
            sample_fps=1.0,
            max_clips=20,
            max_dimension=448,
        )
    if arm == "shot5":
        return "visual", ShotClipRecipe(
            max_duration_seconds=5.0,
            sample_fps=1.0,
            max_clips=20,
            max_dimension=448,
        )
    if arm == "shot10":
        return "visual", ShotClipRecipe(
            max_duration_seconds=10.0,
            sample_fps=1.0,
            max_clips=20,
            max_dimension=448,
        )
    if arm == "audio5":
        return "speech", AudioWindowRecipe(
            duration_seconds=5.0,
            stride_seconds=4.0,
            max_windows=20,
        )
    return None


def timed_encode(
    runtime: EmbeddingGemma2Runtime, items: list[dict[str, Any]], **kwargs: Any
) -> tuple[list[list[float]], float]:
    started = time.perf_counter()
    vectors = runtime.encode(items, **kwargs)
    return vectors, time.perf_counter() - started


async def timing_probe(
    args: argparse.Namespace, corpus: dict[str, Any]
) -> dict[str, Any]:
    fixture_root = args.fixture_root.resolve(strict=True)
    rows = []
    for source_id, filename in (
        ("composite_cfr", "composite-cfr.mkv"),
        ("composite_vfr", "composite-vfr.mkv"),
    ):
        path = fixture_root / "derived" / filename
        started = time.perf_counter()
        frames = await produce(
            path,
            PeriodicFrameRecipe(
                interval_seconds=1.0,
                adjacent_dedup_threshold=None,
                max_frames=50,
                max_dimension=448,
            ),
        )
        dense_frames = await produce(
            path,
            PeriodicFrameRecipe(
                interval_seconds=0.05,
                adjacent_dedup_threshold=None,
                max_frames=500,
                max_dimension=448,
            ),
        )
        clips = await produce(
            path,
            FixedClipRecipe(
                duration_seconds=5.0,
                stride_seconds=4.0,
                sample_fps=1.0,
                max_clips=20,
                max_dimension=448,
            ),
        )
        rows.append(
            {
                "source_id": source_id,
                "sha256": sha256(path),
                "elapsed_seconds": time.perf_counter() - started,
                "frame_points": [item.point.seconds for item in frames.items],
                "dense_frame_points": [
                    item.point.seconds for item in dense_frames.items
                ],
                "clip_intervals": [
                    [item.start_seconds, item.end_seconds] for item in clips.items
                ],
                "clip_frame_points": [
                    [frame.point.seconds for frame in item.frames]
                    for item in clips.items
                ],
                "frame_coverage": asdict(frames.coverage),
                "clip_coverage": asdict(clips.coverage),
            }
        )
    return {
        "schema_version": "video_retrieval_timing_probe.v1",
        "run_id": f"timing-{int(time.time())}",
        "producer_sha": args.producer_sha,
        "runtime_sha": args.runtime_sha,
        "corpus_model": corpus["model"],
        "sources": rows,
    }


async def produced_inputs(
    arm: str, fixture_root: Path
) -> tuple[str, str, list[Any], list[str], dict[str, Any], float]:
    started = time.perf_counter()
    if arm == "order5":
        all_items: list[Any] = []
        source_ids: list[str] = []
        coverages = []
        recipe = FixedClipRecipe(
            duration_seconds=5.0,
            stride_seconds=5.0,
            sample_fps=1.0,
            max_clips=1,
            max_dimension=448,
        )
        for source_id, filename in (
            ("horses_forward", "horses-forward-5s.mkv"),
            ("horses_reversed", "horses-reversed-5s.mkv"),
        ):
            produced = await produce(fixture_root / "derived" / filename, recipe)
            all_items.extend(produced.items)
            source_ids.extend([source_id] * len(produced.items))
            coverages.append({"source_id": source_id, **asdict(produced.coverage)})
        return (
            "order_contrast_visual",
            "native_joint_5s_order_contrast",
            all_items,
            source_ids,
            {"sources": coverages},
            time.perf_counter() - started,
        )
    spec = arm_spec(arm)
    if spec is None:
        raise ValueError(f"arm {arm} does not use produced media")
    family, recipe = spec
    produced = await produce(fixture_root / "derived" / "composite-cfr.mkv", recipe)
    strategy = {
        "probe5": "native_joint_5s_probe",
        "frames5": "standalone_frames_5s",
        "fixed5": "native_joint_fixed_5s_overlap_20pct",
        "fixed10": "native_joint_fixed_10s_overlap_20pct",
        "shot5": "native_joint_shot_aware_max_5s",
        "shot10": "native_joint_shot_aware_max_10s",
        "audio5": "native_audio_5s_overlap_20pct",
    }[arm]
    return (
        family,
        strategy,
        list(produced.items),
        ["composite"] * len(produced.items),
        asdict(produced.coverage),
        time.perf_counter() - started,
    )


async def session_probe(
    args: argparse.Namespace, corpus: dict[str, Any]
) -> dict[str, Any]:
    if args.model_path is None:
        raise ValueError("--model-path is required for the session probe")
    if args.runtime_python is None or args.app_code_root is None:
        raise ValueError("--runtime-python and --app-code-root are required")
    from frisket.runtime.launch import PythonRuntime

    fixture_root = args.fixture_root.resolve(strict=True)
    model_path = args.model_path.resolve(strict=True)
    runtime_python = args.runtime_python.absolute()
    app_code_root = args.app_code_root.resolve(strict=True)
    _, strategy, items, _, coverage, produce_seconds = await produced_inputs(
        "probe5", fixture_root
    )
    clip = items[0]
    if not isinstance(clip, LocatedVideoClip):
        raise RuntimeError("the session probe producer did not return a video clip")
    session = EmbeddingGemma2Session(
        model_path, PythonRuntime(runtime_python, app_code_root)
    )
    space = EmbeddingGemma2Space(
        dimensions=768, image_soft_tokens=70, video_soft_tokens_per_frame=140
    )
    started = time.perf_counter()
    try:
        start_started = time.perf_counter()
        await session._start()
        startup_seconds = time.perf_counter() - start_started
        encode_started = time.perf_counter()
        vectors = await session.encode(
            [clip.input], purpose=EncodingPurpose.DOCUMENT, space=space
        )
        encode_seconds = time.perf_counter() - encode_started
    finally:
        close_started = time.perf_counter()
        await session.aclose()
        close_seconds = time.perf_counter() - close_started
    vector = vectors[0]
    return {
        "schema_version": "video_retrieval_session_probe.v1",
        "run_id": f"session-probe5-{int(time.time())}",
        "producer_sha": args.producer_sha,
        "runtime_sha": args.runtime_sha,
        "model": corpus["model"],
        "strategy": strategy,
        "coverage": coverage,
        "frame_count": len(clip.frames),
        "vector_dimensions": len(vector),
        "vector_norm": math.sqrt(sum(value * value for value in vector)),
        "vector_prefix": list(vector[:8]),
        "cost": {
            "producer_seconds": produce_seconds,
            "sandbox_startup_and_model_load_seconds": startup_seconds,
            "clip_encode_seconds": encode_seconds,
            "clean_close_seconds": close_seconds,
            "total_seconds": time.perf_counter() - started,
            # The model is resident in the sandbox child. Record the parent
            # value accurately here; capture the process-tree peak externally
            # with `/usr/bin/time -v` for the isolated session probe.
            "parent_peak_rss_kib": peak_rss_kib(),
        },
    }


async def run_model_arm(
    args: argparse.Namespace, corpus: dict[str, Any]
) -> dict[str, Any]:
    fixture_root = args.fixture_root.resolve(strict=True)
    if args.model_path is None:
        raise ValueError("--model-path is required for model arms")
    model_path = args.model_path.resolve(strict=True)
    started_total = time.perf_counter()

    transcript_text = next(
        source["transcript"]
        for source in corpus["sources"]
        if source["id"] == "librispeech_1688_142285_0007"
    )
    if args.arm == "transcript":
        family = "speech"
        strategy = "existing_transcript_timed_unit"
        items: list[Any] = []
        source_ids = ["composite"]
        coverage: dict[str, Any] = {
            "source_start_seconds": 4.2,
            "source_end_seconds": 11.26,
            "indexed_spans": [[4.2, 11.26]],
            "complete": True,
            "truncation_reason": None,
        }
        produce_seconds = 0.0
    else:
        (
            family,
            strategy,
            items,
            source_ids,
            coverage,
            produce_seconds,
        ) = await produced_inputs(args.arm, fixture_root)

    load_started = time.perf_counter()
    runtime = EmbeddingGemma2Runtime(model_path)
    load_seconds = time.perf_counter() - load_started

    queries = [query for query in corpus["queries"] if query["family"] == family]
    query_vectors, query_seconds = timed_encode(
        runtime,
        [{"kind": "text", "text": query["text"]} for query in queries],
        purpose="query",
        dimensions=768,
        staging_root=fixture_root,
        image_soft_tokens=70,
        video_soft_tokens_per_frame=140,
    )

    with tempfile.TemporaryDirectory(
        prefix=f"video-retrieval-{args.arm}-", dir=fixture_root.parent / "cache"
    ) as directory:
        staging_root = Path(directory).resolve(strict=True)
        input_bytes = 0
        if args.arm == "transcript":
            document_inputs = [{"kind": "text", "text": transcript_text}]
            locations = [{"source_id": "composite", "start": 4.2, "end": 11.26}]
        else:
            document_inputs = []
            locations = []
            for index, (item, source_id) in enumerate(zip(items, source_ids)):
                staged, staged_bytes = stage_item(item, staging_root, index)
                document_inputs.append(staged)
                locations.append(location(item, source_id))
                input_bytes += staged_bytes
        document_vectors = []
        document_seconds = 0.0
        for document in document_inputs:
            vectors, elapsed = timed_encode(
                runtime,
                [document],
                purpose="document",
                dimensions=768,
                staging_root=staging_root,
                image_soft_tokens=70,
                video_soft_tokens_per_frame=140,
            )
            document_vectors.extend(vectors)
            document_seconds += elapsed

    query_results = [
        {
            "query_id": query["id"],
            "ranking": rank(vector, document_vectors, locations),
        }
        for query, vector in zip(queries, query_vectors)
    ]
    for vector in [*query_vectors, *document_vectors]:
        norm = math.sqrt(sum(float(value) ** 2 for value in vector))
        if not math.isclose(norm, 1.0, rel_tol=1e-4, abs_tol=1e-4):
            raise RuntimeError(f"non-unit embedding norm: {norm}")

    return {
        "schema_version": "video_retrieval_run.v1",
        "run_id": f"{args.arm}-{int(time.time())}",
        "producer_sha": args.producer_sha,
        "runtime_sha": args.runtime_sha,
        "model": corpus["model"],
        "settings": {
            "dimensions": 768,
            "image_soft_tokens": 70,
            "video_soft_tokens_per_frame": 140,
            "torch_threads": 2,
        },
        "arms": [
            {
                "id": args.arm,
                "family": family,
                "strategy": strategy,
                "coverage": coverage,
                "query_results": query_results,
                "cost": {
                    "producer_seconds": produce_seconds,
                    "model_load_seconds": load_seconds,
                    "query_encode_seconds": query_seconds,
                    "document_encode_seconds": document_seconds,
                    "total_seconds": time.perf_counter() - started_total,
                    "peak_rss_kib": peak_rss_kib(),
                    "input_count": len(document_inputs),
                    "input_bytes": input_bytes,
                    "embedding_bytes": len(document_vectors) * 768 * 4,
                },
            }
        ],
    }


async def async_main() -> None:
    args = parse_args()
    corpus = load_corpus(args.corpus)
    if args.arm == "timing":
        result = await timing_probe(args, corpus)
    elif args.arm == "session_probe5":
        result = await session_probe(args, corpus)
    else:
        result = await run_model_arm(args, corpus)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    asyncio.run(async_main())
