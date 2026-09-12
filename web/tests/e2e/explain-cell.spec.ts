// "Explain this cell": an AI column's row drawer can
// expand the EXACT prompt the model saw and the RAW text it returned, read
// from the run's default trace sidecar via the action trace endpoint.
//
// Security: raw model output must render as a TEXT node, never as HTML — the
// project sanitizes markdown cells with DOMPurify, and the explain panel goes
// further by not interpreting markup at all. We prove this by classifying a
// row whose text carries a <script>/<img onerror> payload and asserting the
// payload shows up verbatim in the trace panel with no injected element.

import { execFileSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import { expect, test, type Page } from '@playwright/test';
import {
  clickRunButton,
  createProject,
  importCsv,
  openAction,
  openCellDrawer,
  sheetColumns,
  uniqueName,
} from './helpers';
import {
  actionSelectorResponse,
  stubActionSelectorChoices,
  type SelectorGroupFixture,
} from './selectorChoicesFixture';

// A 3-row CSV. Row 0 carries an XSS probe so we can assert the raw model
// output (which echoes/quotes nothing dangerous, but the PROMPT does contain
// the row text) is rendered inert.
const XSS = `<script>window.__xss_fired=1</script><img src=x onerror="window.__xss_fired=1">`;
const CSV_XSS = XSS.replaceAll('"', '""');
const CSV = `snippet
"The transit authority cut weekend bus service. ${CSV_XSS}"
"A council member's spouse won a $90,000 city contract."
"The county fair opened with record attendance."
`;
const PROMPT = 'Each row is a one-line local news item. Pick the single best label.';
const MODEL = 'gemini/gemini-3.5-flash-lite';

const REPO_ROOT =
  path.basename(process.cwd()) === 'web'
    ? path.resolve(process.cwd(), '..')
    : process.cwd();
const PYTHON = process.env.FRISKET_E2E_PYTHON ?? path.join(REPO_ROOT, '.venv/bin/python');

async function exposeReplayBackedClassifyEngine(page: Page, projectId: string): Promise<void> {
  // This spec runs in replay_strict with a cache it primes itself, so the
  // hosted classify path is available without a live provider credential. Stub
  // only the read-only selector projection; the action catalog, run route, and
  // replay-backed execution remain the live local application.
  const groups: SelectorGroupFixture[] = [{
    id: 'local',
    label: 'Local',
    choices: [{
      choiceId: 'local-semantic',
      label: 'Local semantic',
      summary: 'Runs on this computer',
      authoredSelection: { kind: 'engine', engine: 'local_semantic' },
    }],
  }, {
    id: 'gemini',
    label: 'Gemini',
    choices: [{
      choiceId: 'gemini-gemini-3-5-flash-lite',
      label: 'Gemini 3.5 Flash-Lite',
      summary: 'Replay-backed hosted model',
      authoredSelection: { kind: 'engine_model', engine: 'llm', model: MODEL },
    }],
  }];
  await stubActionSelectorChoices(page, projectId, ({ actionId, field, params }) => {
    expect(actionId).toBe('map.classify');
    expect(field).toBe('engine');
    return actionSelectorResponse({
      projectId,
      actionId,
      field,
      groups,
      currentChoiceId: params.engine === 'llm' && params.model === MODEL
        ? 'gemini-gemini-3-5-flash-lite' : 'local-semantic',
    });
  });
}

function primeExplainCache(pid: string, sheetId: number): void {
  const workspace = process.env.FRISKET_E2E_WS ?? path.join(os.homedir(), '.frisket/e2e-ws');
  const script = `
import json
import sys
from pathlib import Path
from frisket.actions.system import typed_action_for_request
from frisket.ai.llm import LLMRequest, LLMResponse, ResponseCache, request_key
from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
from frisket.engine.store import Project

workspace, pid, sheet_id_raw = sys.argv[1], sys.argv[2], sys.argv[3]
sheet_id = int(sheet_id_raw)
project = Project(Path(workspace) / f"{pid}.frisket")
try:
    action = {
        "action_id": "map.classify",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": ["snippet"],
            "engine": "llm",
            "model": ${JSON.stringify(MODEL)},
            "context": ${JSON.stringify(PROMPT)},
            "fields": [
                {
                    "name": "topic",
                    "type": "category",
                    "labels": ["transit", "money", "other"],
                }
            ],
            # extract-form-polish-v1 item 6: a fresh classify launch now defaults
            # both toggles OFF (was True) — the cache-priming spec's request_key
            # must match whatever the live form actually posts below.
            "include_justification": False,
            "include_confidence": False,
        },
        "output_names": {"topic": "topic"},
        "idempotency_key": "e2e-cache-prime",
    }
    plan = build_typed_map_rows_plan(project, typed_action_for_request(action))
    spec = plan.spec_dict()
    columns = {c["name"]: c["id"] for c in project.columns(sheet_id)}
    values = {
        name: project.get_values(sheet_id, columns[name])
        for name in spec["input_columns"]
    }
    cache = ResponseCache(project.path / "project.cache.db")
    try:
        for row_id in project.visible_row_ids(sheet_id):
            row_values = {name: values[name].get(row_id) for name in ["snippet"]}
            call = plan.program.render(row_values, spec)
            req = LLMRequest(
                model=spec["model"],
                messages=call.messages,
                schema=call.schema,
                max_tokens=call.max_tokens,
            )
            cache.put(
                request_key(req, plan.program.version),
                LLMResponse(
                    content=json.dumps({
                        "topic": "transit",
                        "topic_confidence": 0.91,
                        "topic_justification": "mentions local public service",
                    }),
                    data={
                        "topic": "transit",
                        "topic_confidence": 0.91,
                        "topic_justification": "mentions local public service",
                    },
                    tokens_in=120,
                    tokens_out=30,
                    cost=0.0001,
                    model=spec["model"],
                ),
            )
    finally:
        cache.close()
finally:
    project.close()
`;
  execFileSync(PYTHON, ['-c', script, workspace, pid, String(sheetId)], {
    cwd: REPO_ROOT,
    stdio: 'pipe',
    timeout: 60_000,
  });
}

test('Explain this cell shows the exact prompt + raw output, rendered XSS-safe', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-explain-cache'));
  const sheetId = await importCsv(page.request, pid, `snippets-${Date.now()}.csv`, CSV);
  primeExplainCache(pid, sheetId);
  await exposeReplayBackedClassifyEngine(page, pid);

  await page.goto(`/p/${pid}/s/${sheetId}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 10_000 });
  await expect(page.getByTestId('sheet-stats')).toHaveText('3 rows · 1 column');

  // Run through the real UI with a stable prompt. The project cache is primed
  // with responses for this exact sheet so the browser path still records a
  // trace without depending on a shared, ever-growing fixture project.
  await openAction(page, 'map.classify');
  await expect(page.getByTestId('generated-action-form')).toBeVisible();
  // Classify defaults to the provider-free local semantic engine. This
  // cassette exercises the hosted prompt/raw-output trace, so choose the
  // replay-backed model through the authoritative selector.
  const engineField = page.getByTestId('field-engine');
  const trigger = engineField.locator('.engine-selector__trigger');
  await trigger.click();
  const dialog = page.getByTestId('engine-selector-dialog');
  await dialog.getByRole('searchbox', { name: 'Search Engine' }).fill(MODEL);
  await dialog.locator('[data-engine-selector-choice="gemini-gemini-3-5-flash-lite"]').click();
  await expect(trigger).toContainText('Gemini 3.5 Flash-Lite');
  await page.getByTestId('field-context').fill(PROMPT);
  await page.getByTestId('output-field-name').fill('topic');
  await page.getByLabel('Field 1 labels').fill('transit, money, other');
  const runButton = page.locator(
    '[data-testid="run-button"], [data-testid="generated-action-run"]',
  );
  await expect
    .poll(async () => {
      return (await runButton.isEnabled()) ? 'ready' : 'waiting';
    }, { timeout: 15_000 })
    .toBe('ready');
  // The classify estimate lands above the local confirmation threshold, so
  // the drawer run flow shows the cost gate; the helper's default confirms it.
  await clickRunButton(page);

  await expect(page.getByTestId('run-progress')).toContainText('complete', { timeout: 60_000 });
  // extract-form-polish-v1 item 6: a fresh classify no longer defaults
  // Include confidence/justification ON, so this run creates only "topic"
  // (2 columns total) instead of "topic" + "topic_confidence" +
  // "topic_justification" (4).
  await expect(page.getByTestId('sheet-stats')).toHaveText('3 rows · 2 columns');

  // action-drawer-autoclose-on-start-v1: the overlay action drawer auto-closed
  // when the run started (it no longer stays open after Run), so it does not
  // cover the grid — no manual close needed. Open the row drawer on the AI
  // column for row 0 (the XSS row).
  await expect(page.getByTestId('action-drawer')).toHaveCount(0);
  await expect(page.getByTestId('grid')).toBeVisible();
  const columns = await sheetColumns(page.request, pid, sheetId);
  // Category-cell clicks apply a one-click facet. Use the selected cell's
  // explicit detail affordance rather than relying on keyboard focus after
  // that filtering interaction.
  await openCellDrawer(page, columns, 'topic', 0);
  const drawer = page.getByTestId('row-drawer');
  await expect(drawer).toBeVisible();

  // Scope to the AI field we clicked (Include confidence/justification are
  // off by default, so the classify run writes only the one "topic" AI
  // column, each with its own Explain affordance).
  const topicField = drawer.locator('.row-field', { hasText: 'topic' }).first();

  // Expand the hover-only explain icon: the trace panel appears with the
  // prompt and raw model output sections.
  await topicField.hover();
  await topicField.getByTestId('cell-action-explain').click();
  const panel = topicField.getByTestId('explain-panel');
  await expect(panel).toBeVisible();

  // The exact prompt the model saw — it embeds the row's text (incl. the XSS
  // probe) — renders as text in a <pre>.
  const prompt = panel.getByTestId('explain-prompt');
  await expect(prompt).toBeVisible();
  await expect(prompt).toContainText('transit authority cut weekend bus service');

  // The raw model output renders (a non-empty <pre>); it's the rawest text the
  // provider returned, before parsing.
  const raw = panel.getByTestId('explain-raw');
  await expect(raw).toBeVisible();
  await expect(raw).not.toBeEmpty();

  // Trace metadata reports the model that produced the cell.
  await expect(panel.getByTestId('explain-meta')).toContainText('Classify rows');
  await expect(panel.getByTestId('explain-meta')).toContainText('gemini');

  // XSS-safety: the prompt <pre> literally contains the <script> text the row
  // carried, but it was NOT interpreted — no script ran and no <img>/<script>
  // element was injected into the document from the trace content.
  await expect(prompt).toContainText('<script>');
  expect(await page.evaluate(() => (window as unknown as { __xss_fired?: number }).__xss_fired)).toBeFalsy();
  // No element-node injection: the panel's payload lives only as text. Any
  // <img onerror>/<script> from the trace would have become a real element.
  const injected = await panel.evaluate((el) => {
    const imgs = Array.from(el.querySelectorAll('img')).some((i) =>
      i.getAttribute('onerror')?.includes('__xss_fired'),
    );
    const scripts = el.querySelectorAll('script').length > 0;
    return { imgs, scripts };
  });
  expect(injected.imgs).toBe(false);
  expect(injected.scripts).toBe(false);

  // Copy affordance is present for the prompt.
  await expect(panel.getByTestId('explain-copy-prompt')).toBeVisible();
});
