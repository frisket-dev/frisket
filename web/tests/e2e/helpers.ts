// Shared helpers for the e2e suite. Everything talks to the live stack:
// vite on :5173 (baseURL) proxying /api to the frisket server on :8000.
//
// The sheet grid is a canvas (glide-data-grid) — cells and headers are not in
// the DOM, so row/header interactions are coordinate clicks computed from the
// same width rules the adapter uses (src/api/real.ts toColumnDef: text=240,
// everything else 140; row markers ≈ 40px; header 34px; rows 34px default).

import { createHash, randomUUID } from 'crypto';
import { execFileSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import { crc32, deflateSync } from 'node:zlib';

import { expect, type APIRequestContext, type Locator, type Page } from '@playwright/test';

const REPO_ROOT =
  path.basename(process.cwd()) === 'web'
    ? path.resolve(process.cwd(), '..')
    : process.cwd();

// ---------------------------------------------------------------------------
// Basemap tiles for the deck.gl map specs.
//
// deck.gl's TileLayer decodes each tile via the browser image pipeline; a
// degenerate 1x1 PNG throws "The source image could not be decoded". Serve a
// real, opaque 256x256 PNG (with CORS) so the basemap renders cleanly and the
// console stays free of image-decode errors.

function pngChunk(type: string, data: Buffer): Buffer {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length, 0);
  const body = Buffer.concat([Buffer.from(type, 'ascii'), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(body) >>> 0, 0);
  return Buffer.concat([len, body, crc]);
}

/** A valid, opaque solid-colour PNG (default 256x256) deck.gl can decode. */
export function solidTilePng(
  size = 256,
  rgba: [number, number, number, number] = [221, 232, 238, 255],
): Buffer {
  const signature = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size, 0);
  ihdr.writeUInt32BE(size, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 6; // colour type RGBA
  const row = Buffer.alloc(1 + size * 4); // filter byte 0 + pixels
  for (let x = 0; x < size; x++) {
    row[1 + x * 4] = rgba[0];
    row[1 + x * 4 + 1] = rgba[1];
    row[1 + x * 4 + 2] = rgba[2];
    row[1 + x * 4 + 3] = rgba[3];
  }
  const raw = Buffer.concat(Array.from({ length: size }, () => row));
  return Buffer.concat([
    signature,
    pngChunk('IHDR', ihdr),
    pngChunk('IDAT', deflateSync(raw)),
    pngChunk('IEND', Buffer.alloc(0)),
  ]);
}

/** Route OSM basemap tiles to a decodable in-memory PNG (with CORS) and report
 *  how many tile requests were made, so a spec can assert the basemap loaded. */
export async function mockBasemapTiles(page: Page): Promise<{ count(): number }> {
  const tile = solidTilePng();
  let count = 0;
  // Force the OSM default basemap regardless of any local web/.env.local or
  // build-time config, so the tile mock below always matches and the spec is
  // deterministic. This also exercises the production runtime-injection path
  // (window.__FRISKET_MAP_CONFIG__) that the hosted server uses.
  await page.addInitScript(() => {
    (
      window as unknown as { __FRISKET_MAP_CONFIG__?: Record<string, string> }
    ).__FRISKET_MAP_CONFIG__ = {
      tileUrlTemplate: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
      attribution: '© OpenStreetMap contributors',
    };
  });
  await page.route('https://tile.openstreetmap.org/**', (route) => {
    count += 1;
    return route.fulfill({
      status: 200,
      contentType: 'image/png',
      headers: { 'access-control-allow-origin': '*' },
      body: tile,
    });
  });
  return { count: () => count };
}

export interface WireColumn {
  id: number;
  name: string;
  type: string;
  ai_generated: boolean;
  /** Display hint ('markdown', 'filesize', …) or null. */
  format?: string | null;
}

export interface WireSheet {
  id: number;
  name: string;
  parent_sheet_id: number | null;
  rows: number;
}

export interface WireRow {
  id: number;
  cells: Record<string, unknown>;
  meta?: Record<string, unknown>;
}

export interface WireData {
  columns: WireColumn[];
  rows: WireRow[];
  total: number;
}

// Seeds the standalone contribution-visibility store (workbench-ia-toolbar-diet-v1;
// replaced the retired workspace-preset snapshot). The Window menu and its
// preset machinery are gone; hide/reveal now persists here and is driven by the
// ⌘K palette's visibility commands.
export async function setHiddenContributions(
  page: Page,
  projectId: string,
  hiddenContributionIds: string[],
) {
  await page.addInitScript(
    ({ projectId, hiddenContributionIds }) => {
      localStorage.setItem(
        `frisket:contribution-visibility:${projectId}`,
        JSON.stringify({
          schemaVersion: 'frisket.contribution_visibility.v1',
          hiddenContributionIds,
        }),
      );
    },
    { projectId, hiddenContributionIds },
  );
}

type HelperLegacyActionSpec = {
  schema_version: 'frisket.action.v2';
  kind: string;
  capabilities: string[];
  params: Record<string, unknown>;
  row_scope?: Record<string, unknown>;
  output_intent?: Array<Record<string, unknown>>;
  idempotency_key: string;
};

type HelperRegisteredActionRequest = {
  action_id: string;
  scope: {
    kind: 'project';
  } | {
    kind: 'sheet_rows';
    sheet_id: number;
    row_ids?: number[];
  };
  params: Record<string, unknown>;
  output_names: Record<string, string>;
  idempotency_key: string;
  confirmation?: string;
};

type HelperActionSpec = HelperLegacyActionSpec | HelperRegisteredActionRequest;

type HelperActionResult = {
  schema_version: string;
  status: string;
  run_id?: number | null;
  outputs?: Array<{
    kind?: string;
    name?: string | null;
    row_ids?: number[];
    ref?: Record<string, unknown>;
  }>;
  errors?: Array<{
    message?: string;
    details?: Record<string, unknown>;
  }>;
};

type HelperRunStatus = {
  run?: {
    status?: string;
    public_status?: {
      status?: string;
      live?: boolean;
      error?: string;
    };
  };
};

// ---------------------------------------------------------------------------
// API helpers (page.request inherits the 5173 baseURL, so paths are /api/…)

export async function listProjects(
  request: APIRequestContext,
): Promise<{ id: string; name: string }[]> {
  const res = await request.get('/api/projects');
  expect(res.ok()).toBeTruthy();
  return res.json();
}

export async function projectIdByName(
  request: APIRequestContext,
  name: string,
): Promise<string> {
  const projects = await listProjects(request);
  const p = projects.find((x) => x.name === name);
  if (!p) throw new Error(`seeded project "${name}" not found — run scripts/e2e/seed_demo.py`);
  return p.id;
}

export async function createProject(
  request: APIRequestContext,
  name: string,
): Promise<string> {
  const res = await request.post('/api/projects', { data: { name } });
  expect(res.ok()).toBeTruthy();
  return (await res.json()).id;
}

export async function importCsv(
  request: APIRequestContext,
  pid: string,
  filename: string,
  csv: string,
): Promise<number> {
  const res = await request.post(`/api/projects/${pid}/import/csv`, {
    multipart: {
      file: { name: filename, mimeType: 'text/csv', buffer: Buffer.from(csv) },
    },
  });
  expect(res.ok()).toBeTruthy();
  return (await res.json()).sheet_id;
}

export async function listSheets(
  request: APIRequestContext,
  pid: string,
): Promise<WireSheet[]> {
  const res = await request.get(`/api/projects/${pid}/sheets`);
  expect(res.ok()).toBeTruthy();
  return res.json();
}

export async function sheetColumns(
  request: APIRequestContext,
  pid: string,
  sheetId: number,
): Promise<WireColumn[]> {
  const res = await request.get(`/api/projects/${pid}/sheets/${sheetId}/data?offset=0&limit=0`);
  expect(res.ok()).toBeTruthy();
  return (await res.json()).columns;
}

export async function sheetData(
  request: APIRequestContext,
  pid: string,
  sheetId: number,
  offset = 0,
  limit = 20,
): Promise<WireData> {
  const res = await request.get(
    `/api/projects/${pid}/sheets/${sheetId}/data?offset=${offset}&limit=${limit}`,
  );
  expect(res.ok()).toBeTruthy();
  return res.json();
}

export async function addRow(
  request: APIRequestContext,
  pid: string,
  sheetId: number,
  cells: Record<string, unknown> = {},
): Promise<{ rowId: number; total: number }> {
  const params = {
    sheet_id: sheetId,
    cells,
  };
  const spec: HelperActionSpec = {
    action_id: 'row.add',
    scope: { kind: 'project' },
    params,
    output_names: {},
    idempotency_key: `e2e-helper-row.add@sha256:${randomUUID().replaceAll('-', '')}`,
  };
  const res = await request.post(`/api/projects/${pid}/actions/v1/run`, { data: spec });
  expect(res.ok()).toBeTruthy();
  const result = (await res.json()) as HelperActionResult;
  if (result.schema_version !== 'frisket.action_result.v1') {
    throw new Error(`row.add returned ${result.schema_version}`);
  }
  if (result.status !== 'completed') {
    throw new Error(actionErrorMessage(result));
  }
  const output = result.outputs?.find((candidate) => (
    candidate.kind === 'rows' && candidate.name === 'added_rows'
  ));
  const rowId = output?.row_ids?.[0];
  const total = Number(output?.ref?.total);
  if (typeof rowId !== 'number' || !Number.isInteger(total)) {
    throw new Error('row.add did not return the added row output');
  }
  return { rowId, total };
}

export async function editCells(
  request: APIRequestContext,
  pid: string,
  edits: Array<{ rowId: number; columnId: number; value: unknown }>,
): Promise<void> {
  const params = {
    edits: edits.map((edit) => ({
      row_id: edit.rowId,
      column_id: edit.columnId,
      value: edit.value,
    })),
  };
  const spec: HelperActionSpec = {
    action_id: 'cell.edit',
    scope: { kind: 'project' },
    params,
    output_names: {},
    idempotency_key: `e2e-helper-cell.edit@sha256:${randomUUID().replaceAll('-', '')}`,
  };
  const res = await request.post(`/api/projects/${pid}/actions/v1/run`, { data: spec });
  expect(res.ok()).toBeTruthy();
  const result = (await res.json()) as HelperActionResult;
  if (result.schema_version !== 'frisket.action_result.v1') {
    throw new Error(`cell.edit returned ${result.schema_version}`);
  }
  if (result.status !== 'completed') {
    throw new Error(actionErrorMessage(result));
  }
}

export async function patchColumn(
  request: APIRequestContext,
  pid: string,
  columnId: number,
  patch: { format?: string | null },
): Promise<void> {
  const spec: HelperActionSpec = {
    action_id: 'column.patch',
    scope: { kind: 'project' },
    params: {
      column_id: columnId,
      format: patch.format ?? null,
    },
    output_names: {},
    idempotency_key: `e2e-helper-column.patch@sha256:${randomUUID().replaceAll('-', '')}`,
  };
  const res = await request.post(`/api/projects/${pid}/actions/v1/run`, { data: spec });
  expect(res.ok()).toBeTruthy();
  const result = (await res.json()) as HelperActionResult;
  if (result.schema_version !== 'frisket.action_result.v1') {
    throw new Error(`column.patch returned ${result.schema_version}`);
  }
  if (result.status !== 'completed') {
    throw new Error(actionErrorMessage(result));
  }
}

export async function setColumnType(
  request: APIRequestContext,
  pid: string,
  columnId: number,
  type: string,
): Promise<void> {
  const spec: HelperActionSpec = {
    action_id: 'column.set_type',
    scope: { kind: 'project' },
    params: {
      column_id: columnId,
      type,
    },
    output_names: {},
    idempotency_key: `e2e-helper-column.set_type@sha256:${randomUUID().replaceAll('-', '')}`,
  };
  const res = await request.post(`/api/projects/${pid}/actions/v1/run`, { data: spec });
  expect(res.ok()).toBeTruthy();
  const result = (await res.json()) as HelperActionResult;
  if (result.schema_version !== 'frisket.action_result.v1') {
    throw new Error(`column.set_type returned ${result.schema_version}`);
  }
  if (result.status !== 'completed') {
    throw new Error(actionErrorMessage(result));
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function copyFields(
  source: Record<string, unknown>,
  fields: string[],
): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const field of fields) {
    if (source[field] !== undefined) out[field] = source[field];
  }
  return out;
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`;
  if (isRecord(value)) {
    return `{${Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${stableJson(value[key])}`)
      .join(',')}}`;
  }
  return JSON.stringify(value) ?? 'null';
}

function helperActionKey(
  pid: string,
  kind: string,
  params: Record<string, unknown>,
  outputIntent: Array<Record<string, unknown>>,
  rowScope?: Record<string, unknown>,
): string {
  const digest = createHash('sha256')
    .update(stableJson({
      project_id: pid,
      kind,
      params,
      row_scope: rowScope ?? null,
      output_intent: outputIntent,
    }))
    .digest('hex');
  return `e2e-helper-${kind}@sha256:${digest}`;
}

function helperOutputIntentFromSeedSpec(seedSpec: Record<string, unknown>): Array<Record<string, unknown>> {
  if (Array.isArray(seedSpec.output_intent)) {
    return seedSpec.output_intent.filter(isRecord).map((intent) => ({ ...intent }));
  }
  if (typeof seedSpec.group_label !== 'string' || !seedSpec.group_label.trim()) return [];
  return [{ kind: 'column_group', group_label: seedSpec.group_label.trim() }];
}

const ROW_SCOPED_HELPER_ACTIONS = new Set(['map.ner']);

function copyRowScope(value: unknown): Record<string, unknown> | undefined {
  if (!isRecord(value)) return undefined;
  const selector = isRecord(value.selector) ? { ...value.selector } : value.selector;
  if (isRecord(selector) && isRecord(selector.membership)) {
    selector.membership = { ...selector.membership };
  }
  return { ...value, selector };
}

function helperRowScope(
  kind: string,
  params: Record<string, unknown>,
  suppliedScope: unknown,
): Record<string, unknown> | undefined {
  const rowScope = copyRowScope(suppliedScope);
  if (!ROW_SCOPED_HELPER_ACTIONS.has(kind)) return rowScope;

  const sheetId = params.sheet_id;
  const rowIds = params.row_ids;
  delete params.sheet_id;
  delete params.row_ids;
  if (rowScope) return rowScope;
  if (typeof sheetId !== 'number' || !Number.isInteger(sheetId)) return undefined;
  return {
    sheet_id: sheetId,
    selector: Array.isArray(rowIds)
      ? {
          kind: 'exact_membership',
          membership: { row_ids: [...rowIds] },
        }
      : { kind: 'all_rows' },
  };
}

function helperChallengeParams(params: Record<string, unknown>): Record<string, unknown> {
  const challenge = { ...params };
  const carriedConsent =
    Object.hasOwn(challenge, 'confirmed') ||
    Object.hasOwn(challenge, 'consented_promise_set_hash');
  delete challenge.consented_promise_set_hash;
  if (carriedConsent) challenge.confirmed = false;
  return challenge;
}

function helperActionSpecFromSeedSpec(
  pid: string,
  seedSpec: Record<string, unknown>,
): HelperActionSpec {
  if (typeof seedSpec.action_id === 'string') {
    if (!isRecord(seedSpec.scope) || seedSpec.scope.kind !== 'sheet_rows') {
      throw new Error('runAndWait requires a sheet_rows scope for registered actions');
    }
    if (!isRecord(seedSpec.params) || !isRecord(seedSpec.output_names)) {
      throw new Error('runAndWait requires params and output_names for registered actions');
    }
    if (typeof seedSpec.idempotency_key !== 'string' || !seedSpec.idempotency_key) {
      throw new Error('runAndWait requires an idempotency_key for registered actions');
    }
    return {
      action_id: seedSpec.action_id,
      scope: {
        kind: 'sheet_rows',
        sheet_id: Number(seedSpec.scope.sheet_id),
        ...(Array.isArray(seedSpec.scope.row_ids)
          ? { row_ids: seedSpec.scope.row_ids.map(Number) }
          : {}),
      },
      params: { ...seedSpec.params },
      output_names: Object.fromEntries(
        Object.entries(seedSpec.output_names).map(([key, value]) => [key, String(value)]),
      ),
      idempotency_key: seedSpec.idempotency_key,
      ...(typeof seedSpec.confirmation === 'string'
        ? { confirmation: seedSpec.confirmation }
        : {}),
    };
  }

  if (seedSpec.schema_version === 'frisket.action.v2') {
    const kind = String(seedSpec.kind ?? '');
    const sourceParams = isRecord(seedSpec.params) ? { ...seedSpec.params } : {};
    const rowScope = helperRowScope(kind, sourceParams, seedSpec.row_scope);
    const params = helperChallengeParams(sourceParams);
    const outputIntent = helperOutputIntentFromSeedSpec(seedSpec);
    return {
      schema_version: 'frisket.action.v2',
      kind,
      capabilities: Array.isArray(seedSpec.capabilities)
        ? [...seedSpec.capabilities].map(String)
        : ['project:write'],
      params,
      ...(rowScope ? { row_scope: rowScope } : {}),
      output_intent: outputIntent,
      idempotency_key:
        typeof seedSpec.idempotency_key === 'string' && seedSpec.idempotency_key
          ? seedSpec.idempotency_key
          : helperActionKey(pid, kind, params, outputIntent, rowScope),
    };
  }

  const recipe = String(seedSpec.recipe ?? '');
  let kind: string;
  const capabilities = ['project:write', 'model:complete'];
  let params: Record<string, unknown>;

  switch (recipe) {
    case 'classify':
      kind = 'map.classify';
      params = {
        ...copyFields(seedSpec, [
          'sheet_id',
          'input_columns',
          'input_template',
          'model',
          'context',
          'fields',
          'include_justification',
          'include_confidence',
          'row_ids',
        ]),
        confirmed: false,
      };
      break;
    case 'judge':
      kind = 'map.judge';
      params = {
        ...copyFields(seedSpec, [
          'sheet_id',
          'input_columns',
          'judged_column',
          'model',
          'guidelines',
          'row_ids',
        ]),
        confirmed: false,
      };
      break;
    default:
      throw new Error(`runAndWait cannot translate seed recipe "${recipe}" to a v1 action`);
  }

  const outputIntent = helperOutputIntentFromSeedSpec(seedSpec);
  const rowScope = helperRowScope(kind, params, seedSpec.row_scope);
  return {
    schema_version: 'frisket.action.v2',
    kind,
    capabilities,
    params,
    ...(rowScope ? { row_scope: rowScope } : {}),
    output_intent: outputIntent,
    idempotency_key: helperActionKey(pid, kind, params, outputIntent, rowScope),
  };
}

function actionErrorMessage(result: HelperActionResult): string {
  return result.errors?.find((error) => error.message)?.message ?? 'action run failed';
}

function confirmationHash(result: HelperActionResult): string {
  if (
    result.schema_version !== 'frisket.action_result.v1' ||
    result.status !== 'needs_confirmation'
  ) {
    throw new Error(
      "malformed HTTP 402 needs_confirmation response: expected v1 needs_confirmation result",
    );
  }
  const value = result.errors?.[0]?.details?.promise_set_hash;
  if (typeof value !== 'string' || !value) {
    throw new Error(
      'malformed HTTP 402 needs_confirmation response: expected nonempty errors[0].details.promise_set_hash',
    );
  }
  return value;
}

async function readActionResult(
  response: Awaited<ReturnType<APIRequestContext['post']>>,
  context: string,
): Promise<HelperActionResult> {
  try {
    const result = await response.json();
    if (!isRecord(result)) throw new Error('not an object');
    return result as HelperActionResult;
  } catch (error) {
    throw new Error(`${context}: expected JSON action result`, { cause: error });
  }
}

function canonicalRunStatus(status: HelperRunStatus): {
  status: string | null;
  live: boolean;
  error: string | null;
} {
  const run = status.run ?? {};
  const publicStatus = run.public_status ?? {};
  return {
    status: run.status ?? publicStatus.status ?? null,
    live: publicStatus.live === true,
    error: typeof publicStatus.error === 'string' ? publicStatus.error : null,
  };
}

const LOCAL_QUEUED_REGISTERED_ACTIONS = new Set([
  'map.regex_extract',
  'map.to_geo_point',
]);

/** Run the configured local E2E queue worker for one durable project run.
 * The Playwright backend intentionally does not embed a worker process. */
export function finishLocalQueuedRun(pid: string, runId: number): void {
  const workspace = process.env.FRISKET_E2E_WS ?? path.join(os.homedir(), '.frisket/e2e-ws');
  const script = `
import sys
import time
from pathlib import Path
from frisket.ai.llm import ModelRouter
from frisket.engine.jobs.queue import open_queue
from frisket.engine.jobs.runs import register_project_run_handler
from frisket.engine.jobs.worker import HandlerRegistry, Worker
from frisket.engine.store import Project

workspace, pid, run_id = sys.argv[1], sys.argv[2], int(sys.argv[3])
queue = open_queue(workspace=workspace)
job = queue.get_project_run_job(pid, run_id)
if job is None:
    raise SystemExit(f"queued job for run {run_id} not found")
registry = HandlerRegistry()
register_project_run_handler(
    registry,
    workspace_root=Path(workspace),
    router=ModelRouter(cache=None, cache_mode="off"),
)
worker = Worker(queue, registry, worker_id=f"e2e-run-{run_id}", lease_seconds=600)
if not worker.run_once():
    project = Project(Path(workspace) / f"{pid}.frisket")
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            row = project.db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            active_claims = project.db.execute(
                "SELECT COUNT(*) FROM output_column_claims WHERE run_id=? AND status='active'",
                (run_id,),
            ).fetchone()[0]
            if row is not None and row["status"] == "completed" and active_claims == 0:
                break
            time.sleep(0.1)
        else:
            raise SystemExit(f"queued job for run {run_id} was not runnable")
    finally:
        project.close()
`;
  execFileSync('uv', ['run', 'python', '-c', script, workspace, pid, String(runId)], {
    cwd: REPO_ROOT,
    stdio: 'pipe',
    timeout: 60_000,
  });
}

/** Start a run server-side, answer an exact confirmation challenge when one
 *  exists, and wait for it to finish. Used by specs that need AI results to
 *  exist but aren't testing the run UI. Returns the run id. */
export async function runAndWait(
  request: APIRequestContext,
  pid: string,
  spec: Record<string, unknown>,
): Promise<number> {
  const actionSpec = helperActionSpecFromSeedSpec(pid, spec);
  const endpoint = `/api/projects/${pid}/actions/v1/run`;
  let res = await request.post(endpoint, { data: actionSpec });
  if (res.status() === 402) {
    const challenge = await readActionResult(
      res,
      'malformed HTTP 402 needs_confirmation response',
    );
    const promiseSetHash = confirmationHash(challenge);
    const retrySpec: HelperActionSpec = 'action_id' in actionSpec
      ? { ...actionSpec, confirmation: promiseSetHash }
      : {
          ...actionSpec,
          params: {
            ...actionSpec.params,
            confirmed: true,
            consented_promise_set_hash: promiseSetHash,
          },
        };
    res = await request.post(endpoint, { data: retrySpec });
  }
  expect(res.ok()).toBeTruthy();
  const actionResult = await readActionResult(res, 'action run response');
  if (actionResult.schema_version !== 'frisket.action_result.v1') {
    throw new Error(`action run returned ${actionResult.schema_version}`);
  }
  if (actionResult.status === 'failed' || actionResult.status === 'needs_confirmation') {
    throw new Error(actionErrorMessage(actionResult));
  }
  const rawRunId = actionResult.run_id;
  if (typeof rawRunId !== 'number' || !Number.isInteger(rawRunId)) {
    const actionId = 'action_id' in actionSpec ? actionSpec.action_id : actionSpec.kind;
    throw new Error(`action ${actionId} did not return a run id`);
  }
  const runId = rawRunId;
  if (
    'action_id' in actionSpec &&
    LOCAL_QUEUED_REGISTERED_ACTIONS.has(actionSpec.action_id)
  ) {
    finishLocalQueuedRun(pid, runId);
  }
  for (let i = 0; i < 300; i++) {
    const statusResponse = await request.get(
      `/api/projects/${pid}/actions/runs/${runId}/status`,
    );
    expect(statusResponse.ok()).toBeTruthy();
    const s = canonicalRunStatus((await statusResponse.json()) as HelperRunStatus);
    if (['failed', 'cancelled', 'stalled', 'orphaned'].includes(s.status ?? '')) {
      throw new Error(
        `run ${runId} ${s.status}${s.error ? `: ${s.error}` : ''}`,
      );
    }
    if (s.status !== 'queued' && s.status !== 'running' && !s.live) return runId;
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error(`run ${runId} did not finish`);
}

export async function clickRunButton(
  page: Page,
  options: { requireCostConfirmation?: boolean } = {},
): Promise<void> {
  const requireCostConfirmation = options.requireCostConfirmation ?? true;
  const runButton = page.locator(
    '[data-testid="run-button"], [data-testid="generated-action-run"]',
  );
  await runButton.click();
  if (!requireCostConfirmation) return;
  const modal = page.getByTestId('cost-gate-modal');
  const modalVisible = await modal.isVisible({ timeout: 500 }).catch(() => false);
  if (!modalVisible) return;
  await page.getByTestId('cost-gate-input').fill('confirm');
  await page.getByTestId('cost-gate-confirm').click();
}

// ---------------------------------------------------------------------------
// Grid (canvas) interaction

const MARKER_WIDTH = 40; // glide row-number column
const HEADER_HEIGHT = 34;
const ROW_HEIGHT = 34; // "Default" — fresh contexts have no persisted pref

const colWidth = (c: WireColumn): number => (c.type === 'text' ? 240 : 140);

async function gridBox(page: Page) {
  const grid = page.getByTestId('grid');
  await grid.scrollIntoViewIfNeeded();
  const box = await grid.boundingBox();
  if (!box) throw new Error('grid not visible');
  return box;
}

async function groupHeaderHeight(page: Page): Promise<number> {
  const raw = await page.getByTestId('grid').getAttribute('data-group-header-height');
  const parsed = Number(raw ?? 0);
  return Number.isFinite(parsed) ? parsed : 0;
}

/** Center x (relative to the grid host) of a named column. */
function columnCenterX(columns: WireColumn[], name: string): number {
  let x = MARKER_WIDTH;
  for (const c of columns) {
    const w = colWidth(c);
    if (c.name === name) return x + w / 2;
    x += w;
  }
  throw new Error(`column "${name}" not in [${columns.map((c) => c.name).join(', ')}]`);
}

/** Leading in-cell control x (audio play/pause, relative to the grid host). */
function columnLeadingControlX(columns: WireColumn[], name: string): number {
  let x = MARKER_WIDTH;
  for (const c of columns) {
    if (c.name === name) return x + 20;
    x += colWidth(c);
  }
  throw new Error(`column "${name}" not in [${columns.map((c) => c.name).join(', ')}]`);
}

/** X coordinate for Glide's native header menu hit target. */
function columnMenuX(columns: WireColumn[], name: string): number {
  let x = MARKER_WIDTH;
  for (const c of columns) {
    const w = colWidth(c);
    if (c.name === name) return x + w - 14;
    x += w;
  }
  throw new Error(`column "${name}" not in [${columns.map((c) => c.name).join(', ')}]`);
}

export async function clickCell(
  page: Page,
  columns: WireColumn[],
  columnName: string,
  rowIndex: number,
): Promise<void> {
  const box = await gridBox(page);
  const groupHeight = await groupHeaderHeight(page);
  await page.mouse.click(
    box.x + columnCenterX(columns, columnName),
    box.y + groupHeight + HEADER_HEIGHT + rowIndex * ROW_HEIGHT + ROW_HEIGHT / 2,
  );
}

export async function clickCellLeadingControl(
  page: Page,
  columns: WireColumn[],
  columnName: string,
  rowIndex: number,
): Promise<void> {
  const box = await gridBox(page);
  const groupHeight = await groupHeaderHeight(page);
  await page.mouse.click(
    box.x + columnLeadingControlX(columns, columnName),
    box.y + groupHeight + HEADER_HEIGHT + rowIndex * ROW_HEIGHT + ROW_HEIGHT / 2,
  );
}

/** Toggle a row's selection by clicking its Glide row-marker checkbox in the
 *  left gutter (rowMarkers kind 'both', multi-select). */
export async function selectRow(page: Page, rowIndex: number): Promise<void> {
  const box = await gridBox(page);
  const groupHeight = await groupHeaderHeight(page);
  await page.mouse.click(
    box.x + MARKER_WIDTH / 2,
    box.y + groupHeight + HEADER_HEIGHT + rowIndex * ROW_HEIGHT + ROW_HEIGHT / 2,
  );
}

/** Select a cell then open the row drawer via the floating "open row
 *  details" icon (grid-in-place-edit-v1: double-click/Enter on an eligible
 *  cell now edits it in place instead of opening the drawer, so a plain
 *  text/number/boolean/stars source column needs this — not clickCell+Enter —
 *  to reach the drawer). Safe for ANY column: the icon opens the drawer
 *  regardless of whether the cell happens to be editable. */
export async function openCellDrawer(
  page: Page,
  columns: WireColumn[],
  columnName: string,
  rowIndex: number,
): Promise<void> {
  await clickCell(page, columns, columnName, rowIndex);
  await page.getByTestId('cell-details-float').click();
}

/** Double-click a cell: for allowOverlay cells (e.g. markdown) this opens
 *  Glide's overlay preview in the #portal element. */
export async function dblclickCell(
  page: Page,
  columns: WireColumn[],
  columnName: string,
  rowIndex: number,
): Promise<void> {
  const box = await gridBox(page);
  const groupHeight = await groupHeaderHeight(page);
  await page.mouse.dblclick(
    box.x + columnCenterX(columns, columnName),
    box.y + groupHeight + HEADER_HEIGHT + rowIndex * ROW_HEIGHT + ROW_HEIGHT / 2,
  );
}

/** Open a column's settings drawer. Since workbench-ia-work-views-v1, a header
 *  LABEL click cycles sort (see clickHeaderLabel); the column drawer now opens
 *  via the header menu's "Column settings" item, which this helper drives so the
 *  many specs that just need the drawer keep working. */
export async function clickHeader(
  page: Page,
  columns: WireColumn[],
  columnName: string,
): Promise<void> {
  await clickHeaderMenu(page, columns, columnName);
  await page.getByTestId('grid-column-header-menu').waitFor({ state: 'visible' });
  await page.getByTestId('header-menu-column-settings').click();
}

/** Click a column header's LABEL area (its horizontal center), the region that
 *  drives click-to-sort (workbench-ia-work-views-v1). Distinct from
 *  clickHeaderMenu, which hits the right-edge menu dots. */
export async function clickHeaderLabel(
  page: Page,
  columns: WireColumn[],
  columnName: string,
): Promise<void> {
  const box = await gridBox(page);
  const groupHeight = await groupHeaderHeight(page);
  await page.mouse.click(
    box.x + columnCenterX(columns, columnName),
    box.y + groupHeight + HEADER_HEIGHT / 2,
  );
}

export async function clickHeaderMenu(
  page: Page,
  columns: WireColumn[],
  columnName: string,
): Promise<void> {
  const box = await gridBox(page);
  const groupHeight = await groupHeaderHeight(page);
  await page.mouse.click(
    box.x + columnMenuX(columns, columnName),
    box.y + groupHeight + HEADER_HEIGHT / 2,
  );
}

// Toolbar diet (workbench-ia-toolbar-diet-v1) re-home helpers.
//
// Wrap text · row height · saved views · provenance moved into the toolbar ⋯
// overflow (same testids, relocated). Open it before touching those controls.
export async function openToolbarOverflow(page: Page): Promise<void> {
  await page.getByTestId('toolbar-overflow-button').click();
  await page.getByTestId('toolbar-overflow-menu').waitFor({ state: 'visible' });
}

// The toolbar sort button retired into the column ▾ caret. Filtering now opens
// the Discover/Facets sidebar; sorting retains its compact control panel.
async function openCaretAdvanced(
  page: Page,
  columns: WireColumn[],
  columnName: string,
  itemTestId: string,
  panelTestId: string,
): Promise<void> {
  const menu = page.getByTestId('grid-column-header-menu');
  // The caret click can race with grid settling (virtualized headers shift as
  // rows stream in), so open the menu, confirm the item, then click it.
  for (let attempt = 0; attempt < 3; attempt += 1) {
    await clickHeaderMenu(page, columns, columnName);
    try {
      await menu.waitFor({ state: 'visible', timeout: 3_000 });
      await page.getByTestId(itemTestId).click({ timeout: 3_000 });
      await page.getByTestId(panelTestId).waitFor({ state: 'visible' });
      return;
    } catch {
      // retry: re-open the caret
    }
  }
  await clickHeaderMenu(page, columns, columnName);
  await page.getByTestId(itemTestId).click();
  await page.getByTestId(panelTestId).waitFor({ state: 'visible' });
}

export async function openFriendlyFilterSidebar(
  page: Page,
  columns: WireColumn[],
  columnName: string,
): Promise<void> {
  const menu = page.getByTestId('grid-column-header-menu');
  const facetsPanel = page.getByTestId('friendly-filters-panel');
  for (let attempt = 0; attempt < 3; attempt += 1) {
    if (await facetsPanel.isVisible()) return;
    await clickHeaderMenu(page, columns, columnName);
    try {
      await menu.waitFor({ state: 'visible', timeout: 3_000 });
      await page.getByTestId('header-menu-filter-sidebar').click({ timeout: 3_000 });
      return;
    } catch {
      // The menu closes synchronously when the sidebar opens, so Playwright can
      // observe a detached button even though the click completed.
      if (await facetsPanel.isVisible()) return;
      // retry after the virtualized header settles
    }
  }
  if (await facetsPanel.isVisible()) return;
  await clickHeaderMenu(page, columns, columnName);
  await page.getByTestId('header-menu-filter-sidebar').click();
}

export async function openAdvancedSortPanel(
  page: Page,
  columns: WireColumn[],
  columnName: string,
): Promise<void> {
  await openCaretAdvanced(page, columns, columnName, 'header-menu-advanced-sort', 'grid-sort-panel');
}

export async function clickGroupHeader(
  page: Page,
  columns: WireColumn[],
  firstColumnName: string,
  lastColumnName: string,
): Promise<void> {
  const box = await gridBox(page);
  const groupHeight = await groupHeaderHeight(page);
  const first = columns.find((column) => column.name === firstColumnName);
  if (!first) throw new Error(`column "${firstColumnName}" not found`);
  const x1 = columnCenterX(columns, firstColumnName) - colWidth(first) / 2;
  const last = columns.find((column) => column.name === lastColumnName);
  if (!last) throw new Error(`column "${lastColumnName}" not found`);
  const x2 = columnCenterX(columns, lastColumnName) + colWidth(last) / 2;
  await page.mouse.click(box.x + Math.min(x1 + 72, x2 - 12), box.y + groupHeight / 2);
}

export async function clickGroupHeaderRename(
  page: Page,
  columns: WireColumn[],
  lastColumnName: string,
): Promise<void> {
  const box = await gridBox(page);
  const groupHeight = await groupHeaderHeight(page);
  const last = columns.find((column) => column.name === lastColumnName);
  if (!last) throw new Error(`column "${lastColumnName}" not found`);
  const right = Math.min(columnCenterX(columns, lastColumnName) + colWidth(last) / 2, box.width);
  await page.mouse.move(box.x + right - 13, box.y + groupHeight / 2);
  await page.mouse.click(box.x + right - 13, box.y + groupHeight / 2);
}

// ---------------------------------------------------------------------------
// Navigation

export async function openProject(page: Page, pid: string, sheetId?: number): Promise<void> {
  await page.goto(sheetId ? `/p/${pid}/s/${sheetId}` : `/p/${pid}`);
  await expect(
    page.getByTestId('grid').or(page.getByTestId('import-dropzone')).first(),
  ).toBeVisible({ timeout: 20_000 });
}

/**
 * Open the Import workspace dialog. The sidebar's `import-csv-button`
 * quick-action retired with the left sidebar (workbench-ia-focus-v1); Import now
 * lives in the ribbon Data tab's "Import data…" command.
 */
export async function openImportWorkspace(page: Page): Promise<void> {
  await page.getByTestId('ribbon-tab-data').click();
  await page.getByTestId('ribbon-command-import').click();
}

/**
 * History now lives solely in the bottom dock. Activate its tab and wait for the
 * op-log list to render. Replaces the old sidebar `history-panel-toggle`.
 */
export async function openHistory(page: Page): Promise<void> {
  await page.getByTestId('bottom-dock-tab-history').click();
  await expect(page.getByTestId('history-list')).toBeVisible();
}

/**
 * Activate a Discover-panel tab (workbench-ia-right-edge-v1). The Sources /
 * Facets / Watches / Embeddings / Notifications panels re-homed from the left
 * sidebar into the Discover panel (Notifications split into its own sibling
 * tab per discover-notifications-tab-v1); only the active tab's body renders.
 * Expands the panel from the rail first if it is collapsed.
 */
export async function openDiscoverTab(
  page: Page,
  tab: 'Facets' | 'Mentions' | 'Sources' | 'Watches' | 'Embeddings' | 'Notifications',
): Promise<void> {
  const panel = page.getByTestId('discover-panel');
  const rail = page.getByTestId('discover-rail');
  // Wait for the Discover region to mount as either the panel or the rail
  // (avoids a race where a premature count() reads 0 before React renders).
  await expect(panel.or(rail).first()).toBeVisible();
  if (await rail.isVisible()) {
    await page.getByTestId(`discover-rail-icon-${tab}`).click();
  } else {
    // The tab may sit in the `»` overflow menu when the panel is narrow.
    const tabButton = page.getByTestId(`discover-tab-${tab}`);
    if (await tabButton.isVisible()) {
      await tabButton.click();
    } else {
      await page.getByTestId('discover-tab-overflow').click();
      await page.getByTestId(`discover-tab-menu-${tab}`).click();
    }
  }
  await expect(page.getByTestId(`discover-tab-${tab}`)).toHaveAttribute('aria-selected', 'true');
}

/**
 * Reveal an action tile by walking the tabs actually rendered by the ribbon.
 * The active tab can reset while a project settles, so the scan is retried.
 */
export async function revealRibbonAction(page: Page, launcherKind: string): Promise<Locator> {
  const ribbon = page.getByTestId('act-ribbon');
  const tile = page.getByTestId(`ribbon-action-${launcherKind}`);
  const tabs = ribbon.getByRole('tab');

  await expect(ribbon).toBeVisible({ timeout: 20_000 });
  await expect(async () => {
    if (await tile.isVisible()) return;
    const tabCount = await tabs.count();
    expect(tabCount, 'Act ribbon should expose at least one tab').toBeGreaterThan(0);
    for (let index = 0; index < tabCount; index += 1) {
      await tabs.nth(index).click();
      if (await tile.isVisible()) return;
    }
    throw new Error(`ribbon action "${launcherKind}" is not reachable from its tabs`);
  }).toPass({ timeout: 20_000, intervals: [100, 250, 500] });
  return tile;
}

/** Reveal an action in the compact menu without encoding its current category. */
export async function revealMenuAction(page: Page, launcherKind: string): Promise<Locator> {
  const menubar = page.getByTestId('act-menubar');
  const item = page.getByTestId(`menu-action-${launcherKind}`);
  const triggers = menubar.locator('.act-menubar-trigger');

  await expect(menubar).toBeVisible({ timeout: 20_000 });
  await expect(async () => {
    if (await item.isVisible()) return;
    const triggerCount = await triggers.count();
    expect(triggerCount, 'Act menu should expose at least one category').toBeGreaterThan(0);
    for (let index = 0; index < triggerCount; index += 1) {
      await triggers.nth(index).click();
      if (await item.isVisible()) return;
      await page.keyboard.press('Escape');
    }
    throw new Error(`menu action "${launcherKind}" is not reachable from its categories`);
  }).toPass({ timeout: 20_000, intervals: [100, 250, 500] });
  return item;
}

/** Open an action's form through the Act ribbon, then wait for its drawer. */
export async function openAction(page: Page, launcherKind: string): Promise<void> {
  const tile = await revealRibbonAction(page, launcherKind);
  await tile.click();
  await expect(page.locator(
    '[data-testid="action-form"], [data-testid="generated-action-form"]',
  )).toBeVisible({ timeout: 20_000 });
}

/** Select one or more columns in a generated action's canonical
 * `input_columns` control. The picker intentionally starts empty when an
 * action is launched from the ribbon rather than a column menu. */
export async function selectActionInputColumns(
  page: Page,
  ...columnNames: string[]
): Promise<void> {
  const picker = page.getByTestId('field-input_columns');
  for (const columnName of columnNames) {
    if (await picker.getByRole('button', { name: `Remove ${columnName}` }).count()) continue;
    if (!(await page.getByTestId('field-input_columns-menu').isVisible().catch(() => false))) {
      await picker.click();
    }
    await page
      .getByTestId('field-input_columns-menu')
      .getByRole('option')
      .filter({ hasText: columnName })
      .click();
  }
  if (await page.getByTestId('field-input_columns-menu').isVisible().catch(() => false)) {
    await page.keyboard.press('Escape');
  }
}

export const uniqueName = (prefix: string): string =>
  `${prefix}-${Date.now().toString(36)}-${Math.floor(Math.random() * 1e4)}`;

// ---------------------------------------------------------------------------
// Geo seeding (task e2e-helper-consolidation-v1 / P5). Collapses the two
// independent seedGeoSheet implementations (run-failure-surfacing.spec.ts,
// workbench-ia-work-views.spec.ts) plus the recipe re-inlined verbatim in
// several other geo specs (geo-point, deckgl-map, workbench-availability-a11y,
// workbench-command-palette, workbench-projection-status) into one function.
// `point` is optional because run-failure-surfacing's original impl never
// edited a cell value (an error-toast test — the point column just needs to
// exist and be typed); everything else passes it.

export async function seedGeoSheet(
  page: Page,
  opts: {
    namePrefix?: string;
    filename?: string;
    csv?: string;
    pointColumn?: string;
    point?: { lat: number; lon: number };
  } = {},
): Promise<{ pid: string; sheetId: number; pointId: string }> {
  const namePrefix = opts.namePrefix ?? 'geo-sheet';
  const filename = opts.filename ?? 'places.csv';
  const csv = opts.csv ?? 'place,point\n"Eiffel Tower",\n';
  const pointColumn = opts.pointColumn ?? 'point';
  const pid = await createProject(page.request, uniqueName(namePrefix));
  const sheetId = await importCsv(page.request, pid, filename, csv);
  const columns = await sheetColumns(page.request, pid, sheetId);
  const point = columns.find((c) => c.name === pointColumn);
  if (!point) throw new Error(`column "${pointColumn}" not in seeded geo sheet`);
  await setColumnType(page.request, pid, point.id, 'geo_point');
  if (opts.point) {
    const data = await page.request.get(
      `/api/projects/${pid}/sheets/${sheetId}/data?offset=0&limit=1`,
    );
    expect(data.ok()).toBeTruthy();
    const firstRow = (await data.json()).rows[0] as { id: number };
    await editCells(page.request, pid, [
      { rowId: firstRow.id, columnId: point.id, value: opts.point },
    ]);
  }
  return { pid, sheetId, pointId: String(point.id) };
}

// ---------------------------------------------------------------------------
// textPdf — collapses the two byte-identical-shape PDF builders
// (document-view-screenshots.spec.ts, workbench-ia-document-view.spec.ts),
// which differed only in font size / start position / leading. Defaults match
// workbench-ia-document-view's original values; document-view-screenshots
// passes its original {13, 54, 726, 17} explicitly so neither spec's rendered
// PDF layout changes. The raw %PDF fixture FILES elsewhere are untouched —
// this only consolidates the builder function.
export function textPdf(
  pages: string[][],
  opts: { fontSize?: number; startX?: number; startY?: number; leading?: number } = {},
): Buffer {
  const fontSize = opts.fontSize ?? 12;
  const startX = opts.startX ?? 50;
  const startY = opts.startY ?? 742;
  const leading = opts.leading ?? 14;
  const objects: Array<string | Buffer> = [];
  const pageObjNums = pages.map((_, index) => 4 + 2 * index);
  objects.push('<< /Type /Catalog /Pages 2 0 R >>');
  objects.push(
    `<< /Type /Pages /Kids [${pageObjNums.map((n) => `${n} 0 R`).join(' ')}] /Count ${pages.length} >>`,
  );
  objects.push('<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>');
  pages.forEach((lines, index) => {
    const contentNum = 5 + 2 * index;
    objects.push(
      `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents ${contentNum} 0 R >>`,
    );
    const parts = ['BT', `/F1 ${fontSize} Tf`, `${startX} ${startY} Td`, `${leading} TL`];
    for (const line of lines) {
      parts.push(`(${line.replace(/[\\()]/g, (char) => `\\${char}`)}) Tj`);
      parts.push('T*');
    }
    parts.push('ET');
    const content = Buffer.from(parts.join('\n'), 'latin1');
    objects.push(
      Buffer.concat([
        Buffer.from(`<< /Length ${content.length} >>\nstream\n`, 'latin1'),
        content,
        Buffer.from('\nendstream', 'latin1'),
      ]),
    );
  });
  const chunks = [Buffer.from('%PDF-1.4\n%\xe2\xe3\xcf\xd3\n', 'latin1')];
  const offsets: number[] = [];
  objects.forEach((obj, index) => {
    offsets.push(Buffer.concat(chunks).length);
    chunks.push(Buffer.from(`${index + 1} 0 obj\n`, 'latin1'));
    chunks.push(typeof obj === 'string' ? Buffer.from(obj, 'latin1') : obj);
    chunks.push(Buffer.from('\nendobj\n', 'latin1'));
  });
  const xrefOffset = Buffer.concat(chunks).length;
  chunks.push(Buffer.from(`xref\n0 ${objects.length + 1}\n`, 'latin1'));
  chunks.push(Buffer.from('0000000000 65535 f \n', 'latin1'));
  for (const offset of offsets) {
    chunks.push(Buffer.from(`${String(offset).padStart(10, '0')} 00000 n \n`, 'latin1'));
  }
  chunks.push(
    Buffer.from(
      `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xrefOffset}\n%%EOF\n`,
      'latin1',
    ),
  );
  return Buffer.concat(chunks);
}

// ---------------------------------------------------------------------------
// simplePdf — collapses the seven byte-identical-shape single-page PDF
// builders (workbench-ocr-compare-v2-polish.spec.ts,
// media-to-markdown-v1-execution.spec.ts, workbench-ocr-compare-v2.spec.ts,
// workbench-ocr-compare-v2-screenshots.spec.ts,
// workbench-ocr-compare.spec.ts, ocr-compare-screenshots.spec.ts). Distinct
// shape from textPdf above: single flat `lines: string[]` page, not
// `pages: string[][]`. Six of the seven copies were byte-identical; defaults
// here match those six. ocr-compare-screenshots.spec.ts's copy differed
// (font size / start position / leading) and passes its original
// {12, 54, 726, 16} explicitly so its rendered PDF layout does not change.
export function simplePdf(
  lines: string[],
  opts: { fontSize?: number; startX?: number; startY?: number; leading?: number } = {},
): Buffer {
  const fontSize = opts.fontSize ?? 10;
  const startX = opts.startX ?? 50;
  const startY = opts.startY ?? 742;
  const leading = opts.leading ?? 14;
  const contentParts = ['BT', `/F1 ${fontSize} Tf`, `${startX} ${startY} Td`, `${leading} TL`];
  for (const line of lines) {
    contentParts.push(`(${line.replace(/[\\()]/g, (char) => `\\${char}`)}) Tj`);
    contentParts.push('T*');
  }
  contentParts.push('ET');
  const content = Buffer.from(contentParts.join('\n'), 'latin1');
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [4 0 R] /Count 1 >>',
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>',
    Buffer.concat([
      Buffer.from(`<< /Length ${content.length} >>\nstream\n`, 'latin1'),
      content,
      Buffer.from('\nendstream', 'latin1'),
    ]),
  ];
  const chunks = [Buffer.from('%PDF-1.4\n%\xe2\xe3\xcf\xd3\n', 'latin1')];
  const offsets: number[] = [];
  for (const [index, object] of objects.entries()) {
    offsets.push(Buffer.concat(chunks).length);
    chunks.push(Buffer.from(`${index + 1} 0 obj\n`, 'latin1'));
    chunks.push(typeof object === 'string' ? Buffer.from(object, 'latin1') : object);
    chunks.push(Buffer.from('\nendobj\n', 'latin1'));
  }
  const xrefOffset = Buffer.concat(chunks).length;
  chunks.push(Buffer.from(`xref\n0 ${objects.length + 1}\n`, 'latin1'));
  chunks.push(Buffer.from('0000000000 65535 f \n', 'latin1'));
  for (const offset of offsets) {
    chunks.push(Buffer.from(`${String(offset).padStart(10, '0')} 00000 n \n`, 'latin1'));
  }
  chunks.push(
    Buffer.from(
      `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xrefOffset}\n%%EOF\n`,
      'latin1',
    ),
  );
  return Buffer.concat(chunks);
}

// ---------------------------------------------------------------------------
// openPalette — the ⌘K command palette open, hand-rolled at ~10 call sites
// as `press('ControlOrMeta+k')` + declare the region locator + assert
// visible. Returns the palette locator for the (common) case a spec then
// scopes further queries to it. Does NOT cover the alternate Meta+Shift+P
// entry point workbench-entrypoints-friction.spec.ts exercises on purpose
// (that IS the behavior under test there, not a duplicate to collapse), nor
// the toggle-CLOSE presses that assert the palette is hidden afterward.
export async function openPalette(page: Page) {
  await page.keyboard.press('ControlOrMeta+k');
  const palette = page.getByTestId('workbench-region-commandPalette');
  await expect(palette).toBeVisible();
  return palette;
}

// 5-row CSV used by the mutating specs.
export const TINY_CSV = `snippet
"The transit authority cut weekend service on three bus lines without a public hearing."
"A council member's spouse won a $90,000 landscaping contract from the parks department."
"The county fair opened with record attendance and a new pie-eating champion."
"Auditors say the water department double-paid an engineering vendor for two years."
"The library system added Sunday hours at four branches after a community petition."
`;
