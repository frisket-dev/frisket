import { mkdir, mkdtemp, rename, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';

import type { Page } from '@playwright/test';

import type { WalkthroughMarkdownOptions } from '../../src/walkthrough/guide';
import type { WalkthroughDefinition } from '../../src/walkthrough/walkthroughs';

type WalkthroughStep = WalkthroughDefinition['steps'][number];

interface CaptureStepContext {
  page: Page;
  step: WalkthroughStep;
  stepIndex: number;
  capture: () => Promise<void>;
}

interface PublishWalkthroughOptions {
  page: Page;
  walkthrough: WalkthroughDefinition;
  outputDirectory: string;
  renderMarkdown: (
    walkthrough: WalkthroughDefinition,
    options?: WalkthroughMarkdownOptions,
  ) => string;
  captureStep: (context: CaptureStepContext) => Promise<void>;
  advanceStep: (page: Page, step: WalkthroughStep) => Promise<void>;
}

function imageName(step: WalkthroughStep, stepIndex: number): string {
  if (!/^[a-z0-9-]+$/.test(step.id)) {
    throw new Error(`Unsafe walkthrough step id: ${step.id}`);
  }
  return `${String(stepIndex + 1).padStart(2, '0')}-${step.id}.png`;
}

/**
 * Capture one already-open walkthrough into a sibling staging directory and
 * expose it only after every real step, screenshot, and advance succeeds.
 */
export async function publishWalkthrough({
  page,
  walkthrough,
  outputDirectory,
  renderMarkdown,
  captureStep,
  advanceStep,
}: PublishWalkthroughOptions): Promise<void> {
  const parentDirectory = path.dirname(outputDirectory);
  const outputName = path.basename(outputDirectory);
  await mkdir(parentDirectory, { recursive: true });
  const stagingDirectory = await mkdtemp(path.join(parentDirectory, `.${outputName}.stage-`));
  let published = false;

  try {
    const imageNames = walkthrough.steps.map(imageName);
    if (new Set(imageNames).size !== imageNames.length) {
      throw new Error(`Walkthrough ${walkthrough.id} has duplicate screenshot names`);
    }

    for (const [stepIndex, step] of walkthrough.steps.entries()) {
      const screenshotPath = path.join(stagingDirectory, imageNames[stepIndex]);
      let captured = false;
      await captureStep({
        page,
        step,
        stepIndex,
        capture: async () => {
          if (captured) throw new Error(`Walkthrough step ${step.id} was captured more than once`);
          await page.screenshot({ path: screenshotPath, animations: 'disabled' });
          captured = true;
        },
      });
      if (!captured) throw new Error(`Walkthrough step ${step.id} was not captured`);
      await advanceStep(page, step);
    }

    const markdown = renderMarkdown(walkthrough, {
      imageHrefForStep: (_step, stepIndex) => `./${imageNames[stepIndex]}`,
    });
    await writeFile(path.join(stagingDirectory, 'README.md'), markdown, 'utf8');
    await rename(stagingDirectory, outputDirectory);
    published = true;
  } finally {
    if (!published) await rm(stagingDirectory, { recursive: true, force: true });
  }
}
