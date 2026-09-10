// Shared seeding for the transcribe-deeplink-prefill-v1 e2e spec. Needs
// audio columns with a KNOWN, deterministic transcriptStatus
// (missing|partial|complete_visible|complete_hidden) — the backend signal
// (tests/engine/test_transcript_status_signal.py) is
// row-scoped result-success-based, so reaching complete_visible/
// complete_hidden/partial for real would mean an actual transcribe engine
// run. Instead this seeds the SAME low-level store primitives the passing
// backend pytest (`_seed_media_project` + `run_action_spec` with a
// monkeypatched `FasterWhisperAdapter.transcribe`) already exercises,
// via a `uv run python -c` subprocess against the SAME on-disk project the
// HTTP-created `pid` already owns — the exact pattern
// answers-view-docked-evidence.spec.ts's seedRegionGroundedSheet/
// seedTemporalGroundedSheet establish for constructing realistic backend
// state deterministically without a live model call.

import { execFileSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';

const REPO_ROOT =
  path.basename(process.cwd()) === 'web' ? path.resolve(process.cwd(), '..') : process.cwd();

export type TranscriptStatus = 'missing' | 'partial' | 'complete_visible' | 'complete_hidden';

export interface SeedMediaColumnSpec {
  name: string;
  status: TranscriptStatus;
}

export interface SeededTranscriptStatusSheet {
  sheetId: number;
}

/** Seeds a NEW sheet on the given (already HTTP-created) project with one
 *  audio column per `mediaColumns` entry, each landed at its requested
 *  transcriptStatus. Two rows per media column (needed for the `partial`
 *  axis — row 2's engine call is monkeypatched to fail). `missing` columns
 *  get NO transcribe run at all (the "never ran" case, SS1.3). */
export function seedTranscriptStatusSheet(
  pid: string,
  mediaColumns: SeedMediaColumnSpec[],
  sheetName = 'Media',
): SeededTranscriptStatusSheet {
  const workspace = process.env.FRISKET_E2E_WS ?? path.join(os.homedir(), '.frisket/e2e-ws');
  const specJson = JSON.stringify({ sheetName, mediaColumns });
  const script = String.raw`
import json
import sys
from pathlib import Path

from frisket.engine.executor import run_action_spec
from frisket.engine.store.media_blobs import media_cell, owned_media_metadata_document
from frisket.sdk.ops.transcribe_engines import FasterWhisperAdapter
from frisket.engine.store import Project

workspace, pid, spec_json = sys.argv[1], sys.argv[2], sys.argv[3]
spec = json.loads(spec_json)
project = Project(Path(workspace) / f"{pid}.frisket")
try:
    sheet_id = project.add_sheet(spec["sheetName"])
    col_ids = {}
    blob_pairs = {}
    for col in spec["mediaColumns"]:
        name = col["name"]
        col_ids[name] = project.add_column(sheet_id, name, type="audio")
        blob_a = project.add_blob(
            f"RIFF0000WAVEfmt {name}-a".encode("utf-8"),
            filename=f"{name}_a.wav", mime="audio/wav",
            metadata=owned_media_metadata_document(
                probe={"duration_seconds": 0.5, "kind": "audio"}
            ),
        )
        blob_b = project.add_blob(
            f"RIFF0000WAVEfmt {name}-b".encode("utf-8"),
            filename=f"{name}_b.wav", mime="audio/wav",
            metadata=owned_media_metadata_document(
                probe={"duration_seconds": 0.5, "kind": "audio"}
            ),
        )
        blob_pairs[name] = (blob_a, blob_b)

    row_cells = []
    for i in range(2):
        cells = {}
        for col in spec["mediaColumns"]:
            name = col["name"]
            blob = blob_pairs[name][i]
            cells[name] = media_cell(
                blob, mime="audio/wav", filename=f"{name}_{i}.wav",
            )
        row_cells.append(cells)
    row_ids = project.add_rows(sheet_id, row_cells, col_ids)

    for col in spec["mediaColumns"]:
        name = col["name"]
        status = col["status"]
        if status == "missing":
            continue
        blob_a, blob_b = blob_pairs[name]

        if status == "partial":
            def make_engine(fail_blob=blob_b):
                async def fake(self, path, s, *, should_cancel=None):
                    del self, s, should_cancel
                    if Path(path).name == fail_blob:
                        raise RuntimeError("ASR provider unavailable")
                    return {
                        "text": "hello world", "language": "en", "duration": 0.5,
                        "segments": [{"start": 0.0, "end": 0.5, "text": "hello world"}],
                    }
                return fake
        else:
            def make_engine():
                async def fake(self, path, s, *, should_cancel=None):
                    del self, path, s, should_cancel
                    return {
                        "text": "hello world", "language": "en", "duration": 0.5,
                        "segments": [{"start": 0.0, "end": 0.5, "text": "hello world"}],
                    }
                return fake

        FasterWhisperAdapter.transcribe = make_engine()
        output_name = f"{name}_transcript"
        result = run_action_spec(
            project,
            {
                "schema_version": "frisket.action.v2",
                "kind": "media.transcribe",
                "capabilities": ["project:write", "model:transcribe"],
                "params": {
                    "sheet_id": sheet_id,
                    "input_columns": [name],
                    "engine": "faster_whisper",
                    "output_name": output_name,
                    "row_ids": row_ids,
                    "confirmed": False,
                },
                "idempotency_key": f"e2e-seed-transcript-status@sha256:{pid}:{sheet_id}:{name}",
            },
            project_id="e2e-seed-transcript-status",
        )
        expected = "partial" if status == "partial" else "completed"
        assert result.status == expected, (name, status, result.status)

        if status == "complete_hidden":
            project.db.execute(
                "UPDATE columns SET hidden=1 WHERE sheet_id=? AND name=?",
                (sheet_id, output_name),
            )
            project.db.commit()

    project.db.commit()
    print(json.dumps({"sheetId": sheet_id}))
finally:
    project.close()
`;
  const output = execFileSync('uv', ['run', 'python', '-c', script, workspace, pid, specJson], {
    cwd: REPO_ROOT,
    encoding: 'utf8',
    timeout: 120_000,
  });
  return JSON.parse(output) as SeededTranscriptStatusSheet;
}
