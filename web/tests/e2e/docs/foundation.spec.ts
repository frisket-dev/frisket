import { expect, test } from '@playwright/test';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { createProject, importCsv, openAction, selectActionSourceColumns, uniqueName } from '../helpers';
import { exposeReplayModel, prepareOwnModelReplay } from '../replayModelScenario';
import { captureCheckpoint, publishCaptureBundle, resolveCaptureTarget } from './capture';

test.use({ deviceScaleFactor: 2, viewport: { width: 1600, height: 1000 } });

test('Extract checkpoint captures its real settings and rejects drift', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('docs-foundation'));
  const sheetId = await importCsv(page.request, pid, 'filings.csv', 'filing\n"The court awarded damages."\n');
  await prepareOwnModelReplay(page.request, pid);
  await exposeReplayModel(page, pid);
  await page.goto(`/p/${pid}/s/${sheetId}`);
  await openAction(page, 'map.extract');
  await selectActionSourceColumns(page, 'filing');
  const engine = page.getByTestId('field-engine');
  await engine.locator('.engine-selector__trigger').click();
  await page.locator('[data-engine-selector-choice="replay-engine-model"]').click();
  await expect(engine).toContainText('Gemini 3.5 Flash-Lite');
  await expect(page.getByText('Loading models…')).toHaveCount(0);
  await expect(page.getByText('Loading choices…')).toHaveCount(0);
  await expect(page.getByTestId('run-disabled-reason')).toHaveCount(0);
  const prompt = page.getByTestId('field-instruction');
  const estimate = page.waitForResponse((response) => response.url().endsWith('/actions/v1/estimate') && response.ok());
  await prompt.fill('Identify the amount awarded.');
  await estimate;
  await expect(page.getByTestId('cost-estimate')).toContainText('Estimated cost');
  const panel = page.getByTestId('action-panel');
  const description = { id: 'extract-setup', root: panel, annotations: [{ id: 'instructions',
    target: { kind: 'field' as const, id: 'field-instruction' }, label: 'Instruction',
    expected: { kind: 'value' as const, value: 'Identify the amount awarded.' } }] };
  const parent = await mkdtemp(path.join(os.tmpdir(), 'frisket-docs-foundation-'));
  const output = process.env.FRISKET_DOCS_PROOF_DIR ?? path.join(parent, 'bundle');
  try {
    await expect(captureCheckpoint(page, { ...description, annotations: [{ ...description.annotations[0],
      expected: { kind: 'value', value: 'wrong' } }] }, parent)).rejects.toThrow('expected');
    await publishCaptureBundle(output, { appRevision: process.env.FRISKET_DOCS_REVISION ?? 'development',
      sample: { id: 'foundation-filing', revision: 1 }, replay: 'selector-fixture:gemini-3.5-flash-lite' }, async (stage) => [await captureCheckpoint(page, description, stage)]);
    const bundle = JSON.parse(await readFile(path.join(output, 'bundle.json'), 'utf8'));
    expect(bundle.captures[0].annotations[0].value).toBe('Identify the amount awarded.');
  } finally { await rm(parent, { recursive: true, force: true }); }
});

test('scoped target resolution rejects missing and duplicate visible targets', async ({ page }) => {
  await page.setContent('<section id="scope"><input data-testid="example"><input data-testid="example"></section><input data-testid="example">');
  const root = page.locator('#scope');
  await expect(resolveCaptureTarget(root, { kind: 'field', id: 'example' })).rejects.toThrow('2 visible');
  await root.locator('input').last().evaluate((el) => { el.hidden = true; });
  expect(await (await resolveCaptureTarget(root, { kind: 'field', id: 'example' })).count()).toBe(1);
  await expect(resolveCaptureTarget(root, { kind: 'field', id: 'missing' })).rejects.toThrow('0 visible');
});

test('failed capture leaves the existing bundle intact', async () => {
  const parent = await mkdtemp(path.join(os.tmpdir(), 'frisket-docs-atomic-'));
  const output = path.join(parent, 'bundle');
  const metadata = { appRevision: 'abc', sample: { id: 'sample', revision: 1 } };
  try {
    const { mkdir, writeFile, readdir } = await import('node:fs/promises');
    await mkdir(output);
    await writeFile(path.join(output, 'bundle.json'), 'previous');
    await expect(publishCaptureBundle(output, metadata, async () => { throw new Error('capture failed'); })).rejects.toThrow('capture failed');
    expect(await readFile(path.join(output, 'bundle.json'), 'utf8')).toBe('previous');
    expect(await readdir(parent)).toEqual(['bundle']);
  } finally { await rm(parent, { recursive: true, force: true }); }
});
