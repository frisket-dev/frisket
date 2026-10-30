import { mkdir, mkdtemp, readFile, rename, rm, stat, writeFile } from 'node:fs/promises';
import path from 'node:path';
import type { Locator, Page } from '@playwright/test';
import { walkthroughTargetCandidates } from '../../../src/walkthrough/targets';
import type { WalkthroughTarget } from '../../../src/walkthrough/walkthroughs';
import { setTheme } from '../screenshotHelpers';

export interface CaptureAnnotation {
  id: string;
  /** 1-based position among the recipe step's settings. */
  number?: number;
  target: WalkthroughTarget | { kind: 'radio'; group: string; value: string };
  label: string;
  /** Explicit visible label when the target is a composite control or heading. */
  labelTarget?: Locator;
  expected: { kind: 'value' | 'text' | 'checked'; value: string | boolean };
}
export interface CaptureDescription {
  id: string;
  /** The root is also the element crop; omit for the viewport. */
  root?: Locator;
  /** Optional visible vertical boundaries inside the root, with a little breathing room. */
  cropBetween?: { top: Locator; bottom: Locator };
  annotations: CaptureAnnotation[];
}
export interface CaptureRecord {
  id: string;
  image: string;
  width: number;
  height: number;
  scale: number;
  annotations: Array<{ id: string; number: number; label: string; value: string | boolean;
    rect: { x: number; y: number; width: number; height: number } }>;
}
export interface CaptureBundle {
  version: 1;
  appRevision: string;
  sample: { id: string; revision: number };
  replay?: string;
  captures: CaptureRecord[];
}

/** Require one visible candidate across all representations inside the supplied root. */
export async function resolveCaptureTarget(scope: Page | Locator, target: CaptureAnnotation['target']): Promise<Locator> {
  if (target.kind === 'completed-run') throw new Error('Capture must wait for its exact run ID, not the walkthrough latest-run target');
  const matches: Locator[] = [];
  const selectors = target.kind === 'radio'
    ? [`input[type=radio][name=${JSON.stringify(target.group)}][value=${JSON.stringify(target.value)}]`]
    : walkthroughTargetCandidates(target);
  for (const selector of selectors) {
    let candidates = scope.locator(selector);
    if (target.kind === 'sheet') candidates = candidates.filter({ has: scope.locator('.workbench-mainView-tab-name').filter({ hasText: target.id }) });
    for (let i = 0; i < await candidates.count(); i++) {
      const candidate = candidates.nth(i);
      if (!await candidate.isVisible()) continue;
      if (target.kind === 'sheet' && (await candidate.locator('.workbench-mainView-tab-name').textContent())?.trim() !== target.id) continue;
      if (target.kind === 'local-endpoint' && await candidate.getAttribute('data-endpoint-label') !== target.label) continue;
      matches.push(candidate);
    }
  }
  if (matches.length !== 1) throw new Error(`Capture target ${JSON.stringify(target)} resolved to ${matches.length} visible elements`);
  return matches[0];
}

async function actualValue(locator: Locator, kind: CaptureAnnotation['expected']['kind']): Promise<string | boolean> {
  if (kind === 'checked') return locator.isChecked();
  if (kind === 'value') return locator.inputValue();
  return (await locator.innerText()).trim();
}

/** Capture from the real page only after all targets and claimed values agree. */
export async function captureCheckpoint(page: Page, description: CaptureDescription, directory: string): Promise<CaptureRecord> {
  if (!/^[a-z0-9-]+$/.test(description.id)) throw new Error(`Unsafe capture id: ${description.id}`);
  if (new Set(description.annotations.map((a) => a.id)).size !== description.annotations.length) throw new Error('Duplicate annotation IDs');
  const numbers = description.annotations.map((a, i) => a.number ?? i + 1);
  if (numbers.some((n) => !Number.isSafeInteger(n) || n < 1) || new Set(numbers).size !== numbers.length) throw new Error('Invalid annotation numbers');
  await setTheme(page, 'light');
  await page.evaluate(async () => { await document.fonts.ready; });
  const scale = await page.evaluate(() => window.devicePixelRatio);
  if (scale !== 2) throw new Error(`Capture requires device scale 2, got ${scale}`);
  const scope = description.root ?? page;
  if (description.root) await description.root.scrollIntoViewIfNeeded();
  let crop = description.root ? await description.root.boundingBox() : { x: 0, y: 0, ...page.viewportSize()! };
  if (!crop || crop.width <= 0 || crop.height <= 0) throw new Error(`Capture ${description.id} has no visible crop`);
  if (description.cropBetween) {
    const top = await description.cropBetween.top.boundingBox();
    const bottom = await description.cropBetween.bottom.boundingBox();
    if (!top || !bottom) throw new Error(`Capture ${description.id} has missing crop boundaries`);
    const y = Math.max(crop.y, top.y - 12);
    const end = Math.min(crop.y + crop.height, bottom.y + bottom.height + 12);
    if (end <= y || y < 0 || end > page.viewportSize()!.height) throw new Error(`Capture ${description.id} has invalid crop boundaries`);
    crop = { x: crop.x, y, width: crop.width, height: end - y };
  }
  const annotations: CaptureRecord['annotations'] = [];
  for (const [index, annotation] of description.annotations.entries()) {
    const target = await resolveCaptureTarget(scope, annotation.target);
    const label = annotation.labelTarget
      ? await (async () => {
        const labelTarget = annotation.labelTarget!;
        if (await labelTarget.count() !== 1 || !await labelTarget.isVisible()) throw new Error(`Capture ${description.id} target ${annotation.id}: label must resolve to one visible element`);
        return (await labelTarget.innerText()).trim();
      })()
      : await target.evaluate((element) => {
        const control = element as HTMLInputElement;
        return Array.from(control.labels ?? []).map((item) => (control.type === 'radio' ? item.querySelector('strong')?.textContent : item.textContent)?.trim()).filter(Boolean).join(' ')
          || element.getAttribute('aria-label') || '';
      });
    if (label !== annotation.label) throw new Error(`Capture ${description.id} target ${annotation.id}: expected label ${JSON.stringify(annotation.label)}, got ${JSON.stringify(label)}`);
    const value = await actualValue(target, annotation.expected.kind);
    if (value !== annotation.expected.value) throw new Error(`Capture ${description.id} target ${annotation.id}: expected ${JSON.stringify(annotation.expected.value)}, got ${JSON.stringify(value)}`);
    const box = await target.boundingBox();
    if (!box || box.x < crop.x - 0.5 || box.y < crop.y - 0.5 || box.x + box.width > crop.x + crop.width + 0.5 || box.y + box.height > crop.y + crop.height + 0.5) throw new Error(`Capture target ${annotation.id} is outside its crop`);
    annotations.push({ id: annotation.id, number: numbers[index], label, value,
      rect: { x: (box.x - crop.x) / crop.width, y: (box.y - crop.y) / crop.height, width: box.width / crop.width, height: box.height / crop.height } });
  }
  const image = `${description.id}.png`;
  if (description.cropBetween) await page.screenshot({ path: path.join(directory, image), clip: crop, animations: 'disabled' });
  else if (description.root) await description.root.screenshot({ path: path.join(directory, image), animations: 'disabled' });
  else await page.screenshot({ path: path.join(directory, image), animations: 'disabled' });
  const png = await readFile(path.join(directory, image));
  if (png.length < 24 || !png.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10])) || png.toString('ascii', 12, 16) !== 'IHDR') throw new Error(`Invalid PNG for capture ${description.id}`);
  const width = png.readUInt32BE(16);
  const height = png.readUInt32BE(20);
  if (!width || !height) throw new Error(`Empty PNG for capture ${description.id}`);
  return { id: description.id, image, width, height, scale, annotations };
}

/** Publish only a complete new bundle; an existing destination is never overwritten. */
export async function publishCaptureBundle(outputDirectory: string, metadata: Omit<CaptureBundle, 'version' | 'captures'>,
  capture: (stagingDirectory: string) => Promise<CaptureRecord[]>): Promise<void> {
  const parent = path.dirname(outputDirectory);
  await mkdir(parent, { recursive: true });
  const stage = await mkdtemp(path.join(parent, `.${path.basename(outputDirectory)}.stage-`));
  let published = false;
  try {
    const captures = await capture(stage);
    if (!captures.length || new Set(captures.map((item) => item.id)).size !== captures.length) throw new Error('Capture bundle must contain unique checkpoints');
    for (const item of captures) {
      if (item.image !== `${item.id}.png` || !/^[a-z0-9-]+$/.test(item.id)) throw new Error(`Invalid capture image: ${item.image}`);
      if (!(await stat(path.join(stage, item.image))).isFile()) throw new Error(`Missing capture image: ${item.image}`);
    }
    await writeFile(path.join(stage, 'bundle.json'), JSON.stringify({ version: 1, ...metadata, captures } satisfies CaptureBundle, null, 2) + '\n');
    await rename(stage, outputDirectory);
    published = true;
  } finally {
    if (!published) await rm(stage, { recursive: true, force: true });
  }
}
