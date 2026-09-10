// When a run creates output columns, (a) the grid scrolls
// horizontally to reveal them, (b) a REUSABLE header annotation ('New columns')
// sits over them, and (c) while they populate the annotation carries a slim
// completed/total progress bar — clearing on a terminal state + a short grace,
// or on click-dismiss.
//
// Drives a REAL map.python run (a deterministic local slow worker, no API key)
// on a sheet WIDE enough that the freshly-appended output column starts
// off-screen, so the auto-scroll is observable as a real scrollLeft change on
// glide's own .dvn-scroller. The annotation is DOM in the header overlay band,
// so it is asserted directly by testid (no canvas pixels).

import { expect, test, type Page } from '@playwright/test';
import { createProject, importCsv, openAction, uniqueName } from './helpers';

// Enough wide columns that appending one output column lands past the 1600px
// viewport — so revealing it requires a real horizontal scroll.
function wideCsv(rows: number): string {
  const cols = Array.from({ length: 16 }, (_, i) => `measurement_column_${i}`);
  const header = cols.join(',');
  const body = Array.from({ length: rows }, (_, r) =>
    cols.map((_, c) => `${r}-${c}`).join(','),
  ).join('\n');
  return `${header}\n${body}\n`;
}

async function scrollLeft(page: Page): Promise<number> {
  return page.evaluate(
    () => document.querySelector<HTMLElement>('.dvn-scroller')?.scrollLeft ?? -1,
  );
}

async function newColumnId(page: Page): Promise<string> {
  let id = '';
  await expect
    .poll(async () => {
      id =
        (await page.evaluate(() => {
          const p = (window as unknown as { __frisketLiveFill?: { pendingColumnIds: string[] } })
            .__frisketLiveFill;
          return p?.pendingColumnIds[0] ?? '';
        })) ?? '';
      return id;
    }, { timeout: 20_000, intervals: [100, 150, 200] })
    .not.toBe('');
  return id;
}

async function startPythonRun(page: Page, code: string, columnName: string): Promise<void> {
  await openAction(page, 'map.python');
  await expect(page.getByTestId('field-code')).toBeVisible();
  await page.getByTestId('field-code').fill(code);
  await page.getByTestId('field-output-computed').fill(columnName);
  await page.getByTestId('generated-action-run').click();
}

async function startGroupedPythonRun(page: Page, pid: string): Promise<void> {
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    const action = route.request().postDataJSON() as {
      params: Record<string, unknown>;
    };
    await route.continue({
      postData: JSON.stringify({
        ...action,
        params: {
          ...action.params,
          code: [
            'import time',
            'time.sleep(0.6)',
            "result = {'answer': row['measurement_column_0'], 'score': 1}",
          ].join('\n'),
          return_schema: {
            type: 'object',
            properties: {
              answer: { type: 'string' },
              score: { type: 'integer' },
            },
            required: ['answer', 'score'],
          },
          output_routes: [
            {
              name: 'answer',
              path: '$.answer',
              target: { kind: 'column', type: 'text' },
            },
            {
              name: 'score',
              path: '$.score',
              target: { kind: 'column', type: 'integer' },
            },
          ],
        },
        output_names: { answer: 'grouped_answer', score: 'grouped_score' },
      }),
    });
  });
  await startPythonRun(
    page,
    "result = {'answer': row['measurement_column_0'], 'score': 1}",
    'grouped_answer',
  );
}

test('a grouped annotation clears the group header and keeps dismiss in the top-right', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-grouped-column-annotation'));
  const sheetId = await importCsv(page.request, pid, 'wide.csv', wideCsv(24));
  await page.goto(`/p/${pid}/s/${sheetId}`);
  const grid = page.getByTestId('grid');
  await expect(grid).toBeVisible({ timeout: 15_000 });

  await startGroupedPythonRun(page, pid);

  const annotation = page.getByTestId('grid-column-annotation');
  await expect(annotation).toBeVisible({ timeout: 20_000 });
  await expect(grid).toHaveAttribute('data-native-column-groups', 'true');
  await expect(annotation).toHaveAttribute('data-header-offset', '28');

  const geometry = await annotation.evaluate((element) => {
    const gridElement = document.querySelector<HTMLElement>('[data-testid="grid"]');
    const dismissElement = element.querySelector<HTMLElement>(
      '[data-testid="grid-column-annotation-dismiss"]',
    );
    const rect = (target: Element | null) => target?.getBoundingClientRect().toJSON() ?? null;
    return {
      annotationBox: rect(element),
      gridBox: rect(gridElement),
      dismissBox: rect(dismissElement),
    };
  });
  const { annotationBox, gridBox, dismissBox } = geometry;
  expect(annotationBox).toBeTruthy();
  expect(gridBox).toBeTruthy();
  // The bubble (and its downward caret) now sits above the group-header row;
  // previously its bottom landed 7px inside that 28px row.
  expect(annotationBox!.y + annotationBox!.height).toBeLessThan(gridBox!.y);

  expect(dismissBox).toBeTruthy();
  expect(dismissBox!.x).toBeGreaterThan(annotationBox!.x + annotationBox!.width / 2);
  expect(dismissBox!.y).toBeLessThan(annotationBox!.y + annotationBox!.height / 2);
  expect(dismissBox!.x + dismissBox!.width).toBeLessThan(annotationBox!.x + annotationBox!.width);
});

test('a run creating a new column scrolls it into view, annotates it, shows a populate progress bar, then clears', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-newcols-annotation'));
  const sheetId = await importCsv(page.request, pid, 'wide.csv', wideCsv(18));
  await page.goto(`/p/${pid}/s/${sheetId}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  // The wide sheet starts scrolled fully left.
  expect(await scrollLeft(page)).toBe(0);

  await startPythonRun(page, "import time\ntime.sleep(0.3)\nresult = {'v': row['measurement_column_0']}", 'out');
  const columnId = await newColumnId(page);

  // (b) The "New columns" annotation appears, keyed to the new column id.
  const annotation = page.getByTestId('grid-column-annotation');
  await expect(annotation).toBeVisible({ timeout: 20_000 });
  await expect(annotation).toContainText(/New column/i);
  await expect(annotation).toHaveAttribute('data-column-ids', new RegExp(columnId));

  // (a) The grid auto-scrolled horizontally to reveal the off-screen new column.
  await expect
    .poll(async () => scrollLeft(page), { timeout: 15_000, intervals: [100, 150, 200] })
    .toBeGreaterThan(0);

  // (c) While populating, the annotation carries a slim progress bar reflecting
  // completed/total (data-progress = "completed/total").
  await expect(page.getByTestId('grid-column-annotation-progress')).toBeVisible({ timeout: 15_000 });
  await expect
    .poll(async () => annotation.getAttribute('data-progress'), {
      timeout: 20_000,
      intervals: [100, 150, 200],
    })
    .toMatch(/^\d+\/\d+$/);

  // The annotation clears once the run reaches a terminal state + a short grace.
  await expect(page.getByTestId('grid-column-annotation')).toHaveCount(0, { timeout: 30_000 });
});

test('the new-columns annotation can be dismissed by clicking its dismiss control', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-newcols-dismiss'));
  const sheetId = await importCsv(page.request, pid, 'wide.csv', wideCsv(24));
  await page.goto(`/p/${pid}/s/${sheetId}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  // A longer run so the annotation is reliably still live when we dismiss it.
  await startPythonRun(
    page,
    "import time\ntime.sleep(0.6)\nresult = {'v': row['measurement_column_0']}",
    'out',
  );
  await newColumnId(page);

  const annotation = page.getByTestId('grid-column-annotation');
  await expect(annotation).toBeVisible({ timeout: 20_000 });
  await page.getByTestId('grid-column-annotation-dismiss').click();
  await expect(page.getByTestId('grid-column-annotation')).toHaveCount(0, { timeout: 10_000 });
});
