import { expect, test } from '@playwright/test';
import { createProject, dblclickCell, openProject, sheetColumns, uniqueName } from './helpers';

import { extractRecordedContent } from './textCitationFixtures';

test('Recorded live extraction opens all cited source text with newline and repeated matches', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('text-citation-live-replay'));
  const seeded = extractRecordedContent(pid);
  const columns = await sheetColumns(page.request, pid, seeded.sheetId);
  await openProject(page, pid, seeded.sheetId);
  await dblclickCell(page, columns, 'plaintiff', 0);
  const drawer = page.getByTestId('row-drawer');
  await expect(drawer).toBeVisible();
  const citations = drawer.getByTestId('cell-evidence-open-plaintiff');
  await expect(citations).toHaveCount(seeded.sources.length);
  const seen: string[] = [];
  for (let index = 0; index < seeded.sources.length; index += 1) {
    await citations.nth(index).click();
    const viewer = page.getByTestId('evidence-viewer');
    await expect(viewer).toBeVisible();
    const text = viewer.getByTestId('evidence-text-body');
    const source = await text.textContent();
    expect(source).not.toBeNull();
    expect(seeded.sources).toContain(source);
    seen.push(source!);
    const marks = viewer.getByTestId('evidence-text-highlight');
    const count = await marks.count();
    expect(count).toBeGreaterThanOrEqual(2);
    for (const mark of await marks.allTextContents()) expect(mark.replace(/\s+/gu, ' ')).toBe('Leena Patel');
    await expect(viewer).not.toContainText('no_word_stream');
    await expect(viewer.getByTestId('evidence-viewer-detail-pane')).toHaveCount(0);
    await page.screenshot({ path: test.info().outputPath(`source-${index}.png`) });
    await viewer.getByRole('button', { name: 'Close evidence viewer' }).click();
  }
  expect(new Set(seen)).toEqual(new Set(seeded.sources));
});
