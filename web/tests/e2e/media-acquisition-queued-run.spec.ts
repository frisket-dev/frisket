import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { expect, test } from '@playwright/test';
import { createProject, uniqueName } from './helpers';

// Media acquisition can take long enough that it must use the normal queued
// job path. Backend placement moved
// media.ytdlp_download/media.fetch_url/media.video_frames/media.extract_faces
// off INLINE onto queued_project_run (executor/action_specs.py's
// _QUEUED_PROJECT_RUN_KINDS) -- request returns a queued run handle
// immediately, a worker executes it.
//
// This spec exercises ONE acquisition kind through the REAL backend queued
// path (real HTTP request -> real project.run queue job -> real background
// worker -> real receipt), offline: media.video_frames processes a real,
// tiny, local fixture (tests/fixtures/media/tiny-video.mp4, already used
// elsewhere in this suite for real playback) with real local ffmpeg/ffprobe
// -- no network call of any kind, so no cassette/mock is needed to keep it
// offline. (media.ytdlp_download/media.fetch_url need a live network call
// or a backend-code monkeypatch to go offline, neither of which is reachable
// from a Playwright spec against the real dev server; the equivalent
// request/worker-split proof for THOSE kinds lives in the backend suite:
// tests/engine/test_media_acquisition_queued_placement.py's
// test_media_download_queued_launch_returns_before_yt_dlp_runs.)

const REPO_ROOT =
  path.basename(process.cwd()) === 'web' ? path.resolve(process.cwd(), '..') : process.cwd();
const MEDIA_DIR = path.resolve(process.cwd(), 'tests', 'fixtures', 'media');
const VIDEO_FIXTURE_B64 = readFileSync(path.join(MEDIA_DIR, 'tiny-video.mp4')).toString('base64');

interface SeededVideoRow {
  sheetId: number;
  rowId: number;
  columnName: string;
}

function seedVideoRow(pid: string): SeededVideoRow {
  const workspace = process.env.FRISKET_E2E_WS ?? path.join(os.homedir(), '.frisket/e2e-ws');
  const script = String.raw`
import base64
import json
import sys
from pathlib import Path

from frisket.engine.store.media_blobs import media_cell, owned_media_metadata_document
from frisket.engine.store import Project

workspace, pid, media_b64 = sys.argv[1], sys.argv[2], sys.argv[3]
project = Project(Path(workspace) / f"{pid}.frisket")
try:
    sheet_id = project.add_sheet("Clips")
    column_id = project.add_column(sheet_id, "video", "video")
    media_bytes = base64.b64decode(media_b64)
    blob_hash = project.add_blob(
        media_bytes,
        "clip.mp4",
        "video/mp4",
        metadata=owned_media_metadata_document(probe={"kind": "video"}),
    )
    row_id = project.add_rows(
        sheet_id,
        [
            {
                "video": media_cell(
                    blob_hash,
                    mime="video/mp4",
                    filename="clip.mp4",
                )
            }
        ],
        {"video": column_id},
    )[0]
    project.db.commit()
    print(json.dumps({"sheetId": sheet_id, "rowId": row_id, "columnName": "video"}))
finally:
    project.close()
`;
  const output = execFileSync('uv', ['run', 'python', '-c', script, workspace, pid, VIDEO_FIXTURE_B64], {
    cwd: REPO_ROOT,
    encoding: 'utf8',
    timeout: 120_000,
  });
  return JSON.parse(output) as SeededVideoRow;
}

test('media.video_frames (an acquisition-family kind) runs through the real queued backend, offline, and finishes as a real job with progress', async ({
  request,
}) => {
  const pid = await createProject(request, uniqueName('acq-queued-video-frames'));
  const { sheetId, rowId, columnName } = seedVideoRow(pid);

  const launch = await request.post(`/api/projects/${pid}/actions/v1/run`, {
    data: {
      action_id: 'media.video_frames',
      scope: { kind: 'sheet_rows', sheet_id: sheetId, row_ids: [rowId] },
      output_names: { frames: 'frames' },
      params: {
        source: columnName,
        sampling: { kind: 'count', count: 2 },
      },
      idempotency_key: `acq-queued-video-frames-${pid}`,
    },
  });
  expect(launch.ok()).toBeTruthy();
  const launched = (await launch.json()) as {
    status: string;
    run_id: number | null;
    job_id: number | null;
  };
  // HARD CONSTRAINT: launch returns the run handle immediately -- 'queued',
  // not 'completed' the way INLINE placement always returned synchronously.
  expect(launched.status).toBe('queued');
  expect(launched.run_id).not.toBeNull();
  expect(launched.job_id).not.toBeNull();
  const runId = launched.run_id as number;
  const jobId = launched.job_id as number;
  expect(jobId).toBeGreaterThan(0); // a real queue job id, not the run.inline negative sentinel.

  // Visible as a REAL queued job (project.run kind, positive id) with
  // progress, not the parity union's synthesized run.inline projection.
  let sawQueuedOrRunningJob = false;
  interface WireJob {
    job_id: number;
    kind: string;
    run_id: number | null;
    status: string;
  }
  const deadline = Date.now() + 20_000;
  while (Date.now() < deadline) {
    const listed = await request.get(`/api/projects/${pid}/actions/jobs`);
    expect(listed.ok()).toBeTruthy();
    const jobs = ((await listed.json()).jobs ?? []) as WireJob[];
    const entry = jobs.find((j) => j.run_id === runId);
    if (entry) {
      expect(entry.job_id).toBe(jobId);
      expect(entry.kind).toBe('project.run');
      if (entry.status === 'queued' || entry.status === 'running') {
        sawQueuedOrRunningJob = true;
      }
      if (entry.status === 'done') break;
    }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  expect(sawQueuedOrRunningJob).toBe(true);

  const finalStatus = await request.get(`/api/projects/${pid}/actions/runs/${runId}/status`);
  expect(finalStatus.ok()).toBeTruthy();
  const publicStatus = (await finalStatus.json()).run.public_status;
  expect(publicStatus.status).toBe('completed');
  expect(publicStatus.action_kind).toBe('media.video_frames');

  // Exactly one jobs-listing entry for this run -- the real job, not
  // double-projected via the run.inline negative-id sentinel.
  const after = await request.get(`/api/projects/${pid}/actions/jobs`);
  const afterJobs = ((await after.json()).jobs ?? []) as WireJob[];
  expect(afterJobs.filter((j) => j.run_id === runId)).toHaveLength(1);
  expect(afterJobs.find((j) => j.run_id === runId)?.job_id).toBe(jobId);
});
