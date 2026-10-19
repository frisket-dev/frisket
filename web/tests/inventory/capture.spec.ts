import { execFileSync } from 'node:child_process';
import { appendFileSync, copyFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { expect, test, type Page } from '@playwright/test';
import { createProject, importCsv, openImportWorkspace, openProject } from '../e2e/helpers';
import { captureActionScenes, captureAskAndComparisonScenes, captureInspectionScenes } from './action-scenes';
import { captureImportScenes } from './import-scenes';
import { captureMediaScenes } from './media-scenes';
import { captureMentionsScenes, capturePanelScenes, capturePopulatedPanelScenes, captureSettingsScenes } from './workbench-scenes';
import { captureTeamSettingsScenes } from './team-scenes';

const output = path.resolve('screenshots/inventory');
type Capture = { family: string; name: string; source: string; file: string; detail?: string; theme: string };
const manifest = path.join(output, 'captures.ndjson');
let browserErrors: string[] = [];
const detailSelectors: Record<string, string> = {
  Actions: '[data-testid="action-drawer"]', Settings: '[data-testid="settings-content"]',
  'Team settings': '[data-testid="settings-content"]',
  'Side panels': '[data-testid="discover-panel"]', 'Bottom dock': '[data-testid="bottom-dock-panel"]',
  Import: '[data-testid="import-workspace-dialog"]', 'Import workflow': '[data-testid="import-workspace-dialog"]',
  Ask: '[data-testid="ask-dock"]', Extract: '[data-testid="extract-view"], [data-testid="action-drawer"]',
};

async function shot(page: Page, family: string, name: string, source: string) {
  console.info(`Capture: ${family} / ${name}`);
  await page.evaluate(() => document.fonts.ready);
  for (const theme of ['light', 'dark']) {
    await page.evaluate(value => { document.documentElement.dataset.frisketTheme = value; }, theme);
    const base = `${family}-${name}-${theme}`.toLowerCase().replace(/[^a-z0-9-]+/g, '-');
    const file = `${base}.png`;
    await page.screenshot({ path: path.join(output, file), fullPage: true, animations: 'disabled' });
    let detail: string | undefined;
    if (detailSelectors[family]) {
      const pane = page.locator(detailSelectors[family]).first();
      if (await pane.isVisible()) {
        detail = `${base}-detail.png`;
        await pane.screenshot({ path: path.join(output, detail), animations: 'disabled' });
      }
    }
    appendFileSync(manifest, `${JSON.stringify({ family, name, source, file, detail, theme })}\n`);
  }
}

function html(value: string) {
  return value.replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]!);
}

test.beforeAll(() => mkdirSync(output, { recursive: true }));
test.beforeEach(async ({ context, page }) => {
  browserErrors = [];
  page.on('pageerror', error => browserErrors.push(error.message));
  const allowed = new Set([process.env.FRISKET_INVENTORY_APP_URL, process.env.FRISKET_INVENTORY_STORY_URL, process.env.FRISKET_INVENTORY_TEAM_URL]);
  await context.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (allowed.has(url.origin)) return route.continue();
    await route.abort();
    throw new Error(`Unexpected external inventory request: ${url.origin}${url.pathname}`);
  });
});
test.afterEach(() => expect(browserErrors, 'Uncaught browser errors').toEqual([]));

test.afterAll(() => {
  const captures: Capture[] = existsSync(manifest)
    ? readFileSync(manifest, 'utf8').trim().split('\n').filter(Boolean).map(line => JSON.parse(line)) : [];
  const commit = process.env.FRISKET_INVENTORY_COMMIT ?? execFileSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8' }).trim();
  for (const document of ['COVERAGE.md', 'FINDINGS.md']) copyFileSync(path.resolve('stories', document), path.join(output, document));
  const families = [...new Set(captures.map(item => item.family))];
  writeFileSync(path.join(output, 'index.html'), `<!doctype html><html lang="en"><meta charset="utf-8">
    <title>Frisket UI inventory</title><style>
    body{font:15px system-ui;margin:28px;background:#f4f4f4;color:#222}nav a{margin-right:18px}
    .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(500px,1fr));gap:20px}
    figure{margin:0;background:white;padding:12px}img{width:100%;height:auto}code{overflow-wrap:anywhere}
    </style><h1>Frisket UI inventory</h1><p>Captured ${html(new Date().toISOString())} · commit ${html(commit)}</p>
    <p>This index lists captured examples, not exhaustive coverage. <a href="COVERAGE.md">Coverage and remaining states</a> · <a href="FINDINGS.md">Findings</a>. Synthetic API states are labeled in captions.</p>
    <nav>${families.map((family, index) => `<a href="#family-${index}">${html(family)}</a>`).join('')}</nav>
    ${families.map((family, index) => `<h2 id="family-${index}">${html(family)}</h2><div class="grid">${captures.filter(item => item.family === family).map(item => `<figure><figcaption><strong>${html(item.name)} · ${html(item.theme)}</strong><br><code>${html(item.source)}</code><br><a href="${html(item.file)}">Full screen context</a></figcaption><a href="${html(item.detail ?? item.file)}"><img loading="lazy" src="${html(item.detail ?? item.file)}" alt="${html(item.name)}"></a></figure>`).join('')}</div>`).join('')}
    </html>`);
});

test('shared controls in production styling', async ({ page }) => {
  const storyURL = process.env.FRISKET_INVENTORY_STORY_URL!;
  for (const [id, name, source] of [
    ['inventory-buttons-and-close-controls--shared-and-activity-close', 'close-controls', 'src/components/IconButton.tsx; ActivityProgress.tsx'],
    ['inventory-selects-and-menus--comparison', 'selects', 'src/components/PanelSelect.tsx; PanelPrimitives.tsx'],
    ['inventory-feedback-and-progress--shared-states', 'feedback', 'src/components/PanelPrimitives.tsx; ActivityProgress.tsx'],
    ['inventory-structured-fields--multiple-types', 'structured-fields', 'src/components/action-panel/StructuredFieldsEditor.tsx; OutputFieldsEditor.tsx'],
  ]) {
    await page.goto(`${storyURL}/iframe.html?id=${id}&viewMode=story`);
    await expect(page.locator('.inventory-example').first()).toBeVisible();
    await shot(page, 'Shared controls', name, source);
    if (name === 'selects') {
      await page.getByLabel('Source column', { exact: true }).click();
      await expect(page.getByRole('listbox')).toBeVisible();
      await shot(page, 'Shared controls', 'select-open', source);
    }
  }
  await page.goto(`${storyURL}/iframe.html?id=inventory-selects-and-menus--in-dialog&viewMode=story`);
  await page.getByLabel('Source column', { exact: true }).click();
  await expect(page.getByRole('listbox')).toBeVisible();
  await shot(page, 'Shared controls', 'dialog-select-open', 'src/components/PanelSelect.tsx');
});

test('four import entry surfaces', async ({ page }) => {
  const pid = await createProject(page.request, 'Visual inventory');
  const sheet = await importCsv(page.request, pid, 'records.csv', 'name,amount,notes\nAda,1200,Council meeting\nMax,50,Planning hearing\n');
  await openProject(page, pid, sheet);
  await openImportWorkspace(page);
  for (const [mode, label] of [['csv', 'one-file'], ['files', 'multiple-files'], ['pdf_split', 'split-pdf'], ['feed', 'feed-source']]) {
    await page.getByTestId(`import-mode-${mode}`).click();
    await expect(page.getByTestId(`import-mode-${mode}`)).toHaveAttribute('aria-selected', 'true');
    await shot(page, 'Import', label, 'src/components/ImportCsv.tsx; importWorkspace/');
  }
});

test('representative action forms', async ({ page }) => captureActionScenes(page, shot));
test('inspection and command search', async ({ page }) => captureInspectionScenes(page, shot));
test('local settings suite', async ({ page }) => {
  test.setTimeout(180_000);
  await captureSettingsScenes(page, shot);
});
test('Team organization settings', async ({ page }) => {
  test.setTimeout(180_000);
  await captureTeamSettingsScenes(page, shot);
});
test('workbench panels and dock', async ({ page }) => {
  test.setTimeout(180_000);
  await capturePanelScenes(page, shot);
});
test('populated panel states', async ({ page }) => capturePopulatedPanelScenes(page, shot));
test('grouped entity mentions', async ({ page }) => captureMentionsScenes(page, shot));
test('import workflow stages', async ({ page }) => {
  test.setTimeout(180_000);
  await captureImportScenes(page, shot);
});
test('documents and extraction', async ({ page }) => {
  test.setTimeout(180_000);
  await captureMediaScenes(page, shot);
});
test('Ask and comparisons', async ({ page }) => captureAskAndComparisonScenes(page, shot));

test('dialog treatments', async ({ page }) => {
  for (const [id, selector] of [['delete-rows', '[data-testid="delete-rows-modal"]'],
    ['extract-sheet', 'dialog'], ['cost-gate', '[data-testid="cost-gate-modal"]']]) {
    await page.goto(`${process.env.FRISKET_INVENTORY_STORY_URL}/iframe.html?id=inventory-dialogs--${id}&viewMode=story`);
    await expect(page.locator(selector)).toBeVisible();
    await shot(page, 'Dialogs', id, 'src/components/*Modal.tsx; src/workbench/extract/ExtractSheetNameDialog.tsx');
  }
});

test('timed transcript components', async ({ page }) => {
  for (const kind of ['audio', 'video']) {
    await page.goto(`${process.env.FRISKET_INVENTORY_STORY_URL}/iframe.html?id=inventory-timed-transcript--${kind}&viewMode=story`);
    await expect(page.getByTestId('document-transcript-segment')).toHaveCount(2);
    await expect.poll(() => page.locator(kind).evaluate(element => (element as HTMLMediaElement).readyState)).toBeGreaterThanOrEqual(2);
    await shot(page, 'Transcript components', `${kind}-transcript`, 'src/workbench/TimedTranscript.tsx; synthetic segments, real bundled media');
  }
});
