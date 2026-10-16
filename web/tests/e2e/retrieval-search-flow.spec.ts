import { execFileSync } from 'node:child_process';
import { mkdirSync } from 'node:fs';
import path from 'node:path';

import { expect, test, type Page } from '@playwright/test';

import {
  createProject,
  importCsv,
  openPalette,
  openProject,
  textPdf,
  uniqueName,
} from './helpers';

const REPO_ROOT =
  path.basename(process.cwd()) === 'web'
    ? path.resolve(process.cwd(), '..')
    : process.cwd();
const PYTHON = process.env.FRISKET_TEST_PYTHON;
const SCREENSHOTS = process.env.FRISKET_RETRIEVAL_SCREENSHOTS;

test.use({
  deviceScaleFactor: 2,
  viewport: { width: 1600, height: 900 },
});

function drainLexicalIndex(projectId: string): void {
  if (!PYTHON) throw new Error('FRISKET_TEST_PYTHON must name the shared test interpreter');
  const workspace = process.env.FRISKET_E2E_WS;
  if (!workspace) throw new Error('FRISKET_E2E_WS must name the active test workspace');
  const script = String.raw`
import sys
from pathlib import Path

from frisket.engine.store import Project
from frisket.search_index import drain_index

workspace, project_id = sys.argv[1:3]
project = Project(Path(workspace) / f"{project_id}.frisket")
try:
    print(drain_index(project))
finally:
    project.close()
`;
  execFileSync(PYTHON, ['-c', script, workspace, projectId], {
    cwd: REPO_ROOT,
    env: {
      ...process.env,
      PYTHONDONTWRITEBYTECODE: '1',
      PYTHONPATH: path.join(REPO_ROOT, 'src'),
    },
    encoding: 'utf8',
    timeout: 120_000,
  });
}

async function screenshot(page: Page, name: string): Promise<void> {
  if (!SCREENSHOTS) return;
  mkdirSync(SCREENSHOTS, { recursive: true });
  await page.screenshot({
    path: path.join(SCREENSHOTS, name),
    animations: 'disabled',
  });
}

test('live lexical search returns grouped results and navigates to the exact cell', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('retrieval-search-flow'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'council.csv',
    [
      'headline,body',
      'Budget hearing,"The council approved a harbor repair contract after public testimony."',
      'Library hours,"The library added Sunday hours after a neighborhood petition."',
    ].join('\n'),
  );
  drainLexicalIndex(pid);

  await openProject(page, pid, sheetId);
  const palette = await openPalette(page);
  await palette.getByTestId('search-mode-select').selectOption('lexical');
  const request = page.waitForRequest((candidate) => {
    if (candidate.method() !== 'POST') return false;
    if (new URL(candidate.url()).pathname !== `/api/projects/${pid}/search`) return false;
    const body = candidate.postDataJSON() as {
      mode?: string;
      query?: { kind?: string; text?: string };
    };
    return body.mode === 'lexical'
      && body.query?.kind === 'text'
      && body.query.text === 'harbor';
  });
  await palette.getByTestId('command-palette-input').fill('harbor');
  await request;

  const hit = palette.getByTestId('search-hit').first();
  await expect(hit).toContainText('harbor repair contract');
  await expect(palette.getByTestId('search-supporting-hit')).toHaveCount(0);
  await screenshot(page, 'retrieval-lexical-results@2x.png');

  await hit.click();
  await expect(palette).toBeHidden();
  await expect(page.getByTestId('grid')).toBeVisible();
  await expect(page.locator('[data-testid="grid"] canvas').first()).toBeFocused();
});

test('a real PDF page launches Find similar with an exact page anchor', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('retrieval-document-flow'));
  const imported = await page.request.post(
    `/api/projects/${pid}/import/files?sheet_name=documents`,
    {
      multipart: {
        files: {
          name: 'harbor-report.pdf',
          mimeType: 'application/pdf',
          buffer: textPdf([
            ['Harbor report', 'The first page introduces the filing.'],
            ['Repair appendix', 'The second page contains the repair schedule.'],
          ]),
        },
      },
    },
  );
  expect(imported.ok()).toBeTruthy();
  const sheets = await (await page.request.get(`/api/projects/${pid}/sheets`)).json() as Array<{
    id: number;
    name: string;
  }>;
  const sheet = sheets.find((candidate) => candidate.name === 'documents');
  expect(sheet).toBeTruthy();

  await openProject(page, pid, sheet!.id);
  await page.getByTestId('view-switch-document').click();
  await expect(page.getByTestId('document-pdf')).toHaveAttribute('data-page-count', '2', {
    timeout: 20_000,
  });
  await page.getByTestId('document-page-next').click();
  await expect(page.getByTestId('document-page-indicator')).toContainText('2 / 2');
  await screenshot(page, 'retrieval-document-page@2x.png');

  const similarityRequest = page.waitForRequest((candidate) => {
    if (candidate.method() !== 'POST') return false;
    if (new URL(candidate.url()).pathname !== `/api/projects/${pid}/search`) return false;
    const body = candidate.postDataJSON() as {
      mode?: string;
      query?: {
        kind?: string;
        anchor?: {
          kind?: string;
          location?: { pages?: { start?: number; end?: number } | null };
        };
      };
    };
    return body.mode === 'semantic'
      && body.query?.kind === 'similar'
      && body.query.anchor?.kind === 'cell'
      && body.query.anchor.location?.pages?.start === 2
      && body.query.anchor.location.pages.end === 2;
  });
  await page.getByTestId('document-find-similar').click();
  await similarityRequest;

  const palette = page.getByTestId('workbench-region-commandPalette');
  await expect(palette).toBeVisible();
  const anchor = palette.getByTestId('search-anchor');
  await expect(anchor).toContainText('Similar to page 2');
  await expect(palette.getByTestId('search-mode-select')).toHaveValue('semantic');
  await expect(palette.getByTestId('search-mode-select')).toBeDisabled();
  await screenshot(page, 'retrieval-page-find-similar@2x.png');
});
