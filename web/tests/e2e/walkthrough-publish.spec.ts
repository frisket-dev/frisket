import { existsSync } from 'node:fs';
import { readFile, readdir } from 'node:fs/promises';
import path from 'node:path';

import { expect, test, type Page } from '@playwright/test';

import { walkthroughToMarkdown } from '../../src/walkthrough/guide';
import {
  REGEX_EXTRACT_WALKTHROUGH,
  type WalkthroughDefinition,
} from '../../src/walkthrough/walkthroughs';
import { setTheme, SHOT_VIEWPORT } from './screenshotHelpers';
import { publishWalkthrough } from './walkthroughPublisher';

type WalkthroughStep = WalkthroughDefinition['steps'][number];
type CaptureStep = (context: {
  page: Page;
  step: WalkthroughStep;
  stepIndex: number;
  capture: () => Promise<void>;
}) => Promise<void>;

const walkthrough = REGEX_EXTRACT_WALKTHROUGH;
const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

const highlightedTarget: Record<string, string | RegExp> = {
  'open-dispatches': /^workbench-mainView-tab-/,
  'open-dispatches-grid': 'view-switch-grid',
  'inspect-story-column': 'grid-column-story',
  'open-extract': 'ribbon-tab-analyze',
  'open-regex': 'ribbon-action-map.regex_extract',
  'configure-regex': 'field-pattern',
  'run-action': 'generated-action-run',
  'review-results': 'grid',
  'open-analyze': 'ribbon-tab-tables',
  'open-join': 'ribbon-action-derive.join',
  'configure-join': 'field-join_right_sheet',
  'run-join': 'generated-action-run',
  'review-join': 'grid',
};

function imageName(step: WalkthroughStep, stepIndex: number): string {
  return `${String(stepIndex + 1).padStart(2, '0')}-${step.id}.png`;
}

async function advanceRegexStep(page: Page, step: WalkthroughStep): Promise<void> {
  if (step.fill) {
    if (step.id === 'configure-join') {
      await expect(page.getByTestId('field-join_right_sheet')).toBeEnabled({ timeout: 10_000 });
    }
    await page.getByTestId('walkthrough-fill-for-me').click();
    return;
  }

  if (step.advance === 'click-target') {
    const targetTestId = await page
      .getByTestId('walkthrough-target-box')
      .getAttribute('data-target-testid');
    expect(targetTestId, `${step.id} must expose its real highlighted control`).toBeTruthy();
    if (targetTestId === 'generated-action-run') {
      await expect(page.getByTestId(targetTestId)).toBeEnabled({ timeout: 10_000 });
    }
    await page.getByTestId(targetTestId!).click();
    return;
  }

  const buttonName = step.id === 'review-join' ? 'Finish' : 'Next';
  await page.getByTestId('walkthrough-card').getByRole('button', { name: buttonName }).click();
}

const captureVisibleStep: CaptureStep = async ({ page, step, stepIndex, capture }) => {
  const card = page.getByTestId('walkthrough-card');
  await expect(card.getByRole('heading', { name: step.title, exact: true })).toBeVisible();
  await expect(card.locator('.walkthrough-progress')).toHaveText(
    `${stepIndex + 1} of ${walkthrough.steps.length}`,
  );

  const targetBox = page.getByTestId('walkthrough-target-box');
  await expect(targetBox, `${step.id} must be capture-ready, not on the Next fallback`).toBeVisible({
    timeout: step.target.kind === 'completed-run' ? 30_000 : 10_000,
  });
  await expect(targetBox).toHaveAttribute('data-target-testid', highlightedTarget[step.id]);
  await capture();
};

test.describe.configure({ mode: 'serial', retries: 0 });
test.use({ viewport: SHOT_VIEWPORT });

test('publishes the deterministic Regex extract walkthrough as ordered Markdown and PNGs', async ({
  page,
}, testInfo) => {
  test.setTimeout(120_000);

  await page.goto('/');
  await page.getByTestId('try-sample-project').click();
  await expect(page).toHaveURL(/\/p\/[^/]+/, { timeout: 30_000 });
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 30_000 });
  await setTheme(page, 'light');
  await expect(page.getByTestId('walkthrough-chooser')).toBeVisible();
  await page.getByTestId('walkthrough-choice-regex-extract').click();
  await expect(page.getByRole('heading', { name: walkthrough.steps[0].title })).toBeVisible();

  const stuckOutput = testInfo.outputPath('stuck-regex-extract');
  const stuckCapture: CaptureStep = async () => {
    throw new Error('walkthrough step never became capture-ready');
  };
  await expect(
    publishWalkthrough({
      page,
      walkthrough,
      outputDirectory: stuckOutput,
      renderMarkdown: walkthroughToMarkdown,
      captureStep: stuckCapture,
      advanceStep: advanceRegexStep,
    }),
  ).rejects.toThrow('walkthrough step never became capture-ready');
  expect(existsSync(stuckOutput)).toBe(false);

  const outputDirectory = testInfo.outputPath('regex-extract');
  await publishWalkthrough({
    page,
    walkthrough,
    outputDirectory,
    renderMarkdown: walkthroughToMarkdown,
    captureStep: captureVisibleStep,
    advanceStep: advanceRegexStep,
  });

  const markdown = await readFile(path.join(outputDirectory, 'README.md'), 'utf8');
  const writtenImages = [...markdown.matchAll(/^!\[(.+)]\(\.\/(\d{2}-[a-z0-9-]+\.png)\)$/gm)];
  expect(writtenImages).toHaveLength(walkthrough.steps.length);
  const expectedFiles = [
    'README.md',
    ...walkthrough.steps.map((step, stepIndex) => imageName(step, stepIndex)),
  ].sort();
  expect((await readdir(outputDirectory)).sort()).toEqual(expectedFiles);

  let previousImagePosition = -1;
  const plainSections = walkthroughToMarkdown(walkthrough)
    .split(/(?=^## \d+\. )/m)
    .map((section) => section.trim())
    .filter(Boolean);
  for (const section of plainSections) expect(markdown).toContain(section);

  for (const [stepIndex, step] of walkthrough.steps.entries()) {
    const fileName = imageName(step, stepIndex);
    const heading = `## ${stepIndex + 1}. ${step.title}`;
    const image = `![${stepIndex + 1}. ${step.title}](./${fileName})`;
    const headingPosition = markdown.indexOf(heading);
    const imagePosition = markdown.indexOf(image);
    expect(headingPosition).toBeGreaterThan(previousImagePosition);
    expect(imagePosition).toBeGreaterThan(headingPosition);
    previousImagePosition = imagePosition;

    const png = await readFile(path.join(outputDirectory, fileName));
    expect(png.length, `${fileName} must not be empty`).toBeGreaterThan(24);
    expect(png.subarray(0, PNG_SIGNATURE.length).equals(PNG_SIGNATURE)).toBe(true);
    expect(png.readUInt32BE(16)).toBe(SHOT_VIEWPORT.width);
    expect(png.readUInt32BE(20)).toBe(SHOT_VIEWPORT.height);
  }
});
