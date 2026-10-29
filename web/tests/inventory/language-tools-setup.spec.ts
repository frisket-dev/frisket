import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { expect, test } from '@playwright/test';
import { createProject, importCsv } from '../e2e/helpers';
import { selectorChoicesResponse } from '../e2e/selectorChoicesFixture';

const PAIR_DISCLOSURE = 'Downloads the required language model on first use.';
for (const engine of [
  { id: 'spacy', action: 'map.ner', label: 'spaCy named-entity extraction (local, fixed labels)',
    display: 'spaCy', setup: 'engine-setup:spacy.local@1', phase: 'Installing the spaCy runtime',
    missing: 'spaCy is not installed. Set up spaCy in the engine picker. For a command-line installation, install frisket-data[standard].' },
  { id: 'opus_mt', action: 'map.translate', label: 'Opus-MT local translation (per-language-pair)',
    display: 'Opus MT', setup: 'engine-setup:opus-mt.local@1', phase: 'Installing local translation components',
    missing: 'Set up local translation in the engine picker before running it.' },
]) test(`${engine.display} uses existing setup and preserves model download disclosures`, async ({ page, context }) => {
  await context.route('**/*', route => new URL(route.request().url()).origin === process.env.FRISKET_INVENTORY_APP_URL
    ? route.continue() : route.abort());
  const pid = await createProject(page.request, `${engine.display} setup`);
  const sid = await importCsv(page.request, pid, 'reporting.csv', 'text\n"Alice met the council in London."\n');
  const output = path.resolve('screenshots/language-tools-setup');
  mkdirSync(output, { recursive: true });
  let state: 'missing' | 'working' | 'ready' = 'missing';
  let starts = 0;
  let runs = 0;
  const pull = () => ({ schemaVersion: 'frisket.model_pull.v4', id: 7003, operation_kind: 'engine_setup',
    display_name: engine.display, model: engine.setup, status: state === 'ready' ? 'done' : 'running',
    capabilities: { cancel: state !== 'ready', retry: false, remove: false }, endpoint_id: null,
    endpoint_origin: null, cancel_requested: false, error: null, created_at: '2026-10-29T00:00:00Z',
    started_at: '2026-10-29T00:00:01Z', finished_at: state === 'ready' ? '2026-10-29T00:01:00Z' : null,
    initiated_by: null, phase: state === 'ready' ? 'done' : engine.phase,
    completed_bytes: 0, total_bytes: null, resolved_digest: null, resolved_size: null, artifact: null });
  await page.route(`**/api/projects/${pid}/selector-choices`, route => {
    const subject = route.request().postDataJSON().subject;
    const response = selectorChoicesResponse({ projectId: pid, subject, currentChoiceId: engine.id,
      groups: [{ id: 'local', label: 'Local', choices: [{ choiceId: engine.id, label: engine.label,
        authoredSelection: { kind: 'engine', engine: engine.id },
        status: state === 'ready' ? 'ready' : state === 'working' ? 'working' : 'needs_setup',
        canAuthor: true, canRun: state === 'ready', blocker: state === 'ready' ? null
          : state === 'working' ? 'Engine setup is still in progress.' : engine.missing,
      }] }] });
    Object.assign(response.groups[0].choices[0], { is_default: true,
      active_operation: state === 'working' ? pull() : null,
      setup: state === 'ready'
        ? engine.id === 'opus_mt' ? { kind: 'first_use_download', disclosure: PAIR_DISCLOSURE } : null
        : { kind: 'engine_setup', scope: 'workspace', setup_ref: engine.setup,
          can_mutate: true, can_start: state === 'missing', blocked_by_operation: null } });
    return route.fulfill({ json: response });
  });
  await page.route('**/api/providers/models/setup', route => {
    starts += 1; state = 'working';
    return route.fulfill({ json: { pull: pull(), deduplicated: false } });
  });
  await page.route('**/api/providers/models/pulls/7003', route => route.fulfill({ json: pull() }));
  await page.route(`**/api/projects/${pid}/actions/v1/run`, route => { runs += 1; return route.abort(); });
  await page.goto(`/p/${pid}/s/${sid}/action/${engine.action}`);
  const form = page.getByTestId('generated-action-form');
  const run = page.getByTestId('generated-action-run');
  const trigger = form.getByTestId('engine-selector').getByRole('button');
  await trigger.click();
  const dialog = page.getByTestId('engine-selector-dialog');
  await dialog.getByRole('button', { name: new RegExp(engine.display === 'spaCy' ? 'spaCy' : 'Opus-MT') }).first().click();
  if (engine.id === 'opus_mt') {
    await dialog.getByRole('button', { name: 'Close Engine selector' }).click();
    await form.getByTestId('translate-pair-source').click();
    await page.getByTestId('translate-pair-source-menu').getByRole('option', { name: 'English', exact: true }).click();
    await form.getByTestId('translate-pair-target').click();
    await page.getByTestId('translate-pair-target-menu').getByRole('option', { name: 'Spanish', exact: true }).click();
    await trigger.click();
  }
  const start = dialog.getByRole('button', { name: 'Download and set up', exact: true });
  await expect(start).toBeVisible();
  await expect(run).toBeDisabled();
  await page.screenshot({ path: path.join(output, `${engine.id}-01-setup.png`) });
  expect(starts).toBe(0);
  await start.click();
  await expect(dialog.getByText(engine.phase)).toBeVisible();
  await expect(dialog.getByRole('progressbar')).not.toHaveAttribute('aria-valuenow');
  await expect(dialog.locator('.model-pull-progress-size')).toHaveCount(0);
  await page.screenshot({ path: path.join(output, `${engine.id}-02-installing.png`) });
  state = 'ready';
  await expect(dialog.getByText('Ready', { exact: true })).toBeVisible();
  await expect(run).toBeEnabled();
  await expect(dialog.getByRole('button', { name: 'Download and set up', exact: true })).toHaveCount(0);
  if (engine.id === 'opus_mt') {
    await expect(dialog.getByText(PAIR_DISCLOSURE, { exact: true })).toBeVisible();
    await expect(form.getByTestId('translate-pair-source')).toContainText('English');
    await expect(form.getByTestId('translate-pair-target')).toContainText('Spanish');
  } else await expect(dialog.getByText(PAIR_DISCLOSURE, { exact: true })).toHaveCount(0);
  await page.screenshot({ path: path.join(output, `${engine.id}-03-ready.png`) });
  expect(starts).toBe(1);
  expect(runs).toBe(0);
});
