import { expect, test } from '@playwright/test';
import type { AskEvent, AskThread, AskTurn } from '../../src/api/projectQA';
import { createProject, importCsv, uniqueName } from './helpers';
import { selectorChoicesResponse, stubSelectorChoices } from './selectorChoicesFixture';

test('Ask stays docked, keeps its draft, and reconnects to saved progress', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-project-ask'));
  const sheetId = await importCsv(page.request, pid, 'stories.csv', 'story\nThe council approved the contract.\nThe school opened.\n');
  const otherSheetId = await importCsv(page.request, pid, 'council audio.csv', 'recording\nCouncil meeting\n');
  await stubSelectorChoices(page, pid, (subject) => selectorChoicesResponse({
    projectId: pid, subject, currentChoiceId: 'test-model', groups: [{
      id: 'local', label: 'On this computer', choices: [{ choiceId: 'test-model', label: 'Test model',
        authoredSelection: { kind: 'model', model: 'ollama/test' } }],
    }],
  }));
  const thread: AskThread = { id: 'thread', title: 'What changed?', scope: { kind: 'sources', sources: [{ kind: 'sheet', sheet_id: sheetId }] },
    model: 'ollama/test', web: false, suggest_actions: true, revision: 1, created_by: null, created_at: '', updated_at: '' };
  let exists = false;
  let active = false;
  let polls = 0;
  const events: { thread_id: string; turn_id: string; seq: number; kind: string; payload: object; created_at: string }[] = [];
  const turnWire = (): AskTurn => ({
    id: 'turn', thread_id: thread.id, request_id: 'request', question: 'What changed?',
    status: 'running', submitted_by: null, started_at: '', finished_at: null,
    usage: null, cost_actual: null, error_summary: null,
    scope: thread.scope, model: thread.model, web: thread.web, suggest_actions: thread.suggest_actions,
  });
  await page.route(`**/api/projects/${pid}/qa/threads**`, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const tail = url.pathname.split('/qa/threads')[1];
    if (!tail && request.method() === 'GET') return route.fulfill({ json: exists ? [thread] : [] });
    if (!tail && request.method() === 'POST') { exists = true; return route.fulfill({ json: thread }); }
    if (tail === '/thread/turns') {
      const body = request.postDataJSON();
      expect(body.scope).toEqual(thread.scope);
      expect(body.question).toBe('What changed?');
      active = true;
      events.push({ thread_id: 'thread', turn_id: 'turn', seq: 1, kind: 'question', payload: { question: body.question }, created_at: '' });
      events.push({ thread_id: 'thread', turn_id: 'turn', seq: 2, kind: 'tool_started', payload: { tool: 'inspect_sheets' }, created_at: '' });
      return route.fulfill({ json: turnWire() });
    }
    if (tail === '/thread/events') {
      if (active && ++polls >= 2) {
        active = false;
        events.push({ thread_id: 'thread', turn_id: 'turn', seq: 3, kind: 'tool_completed', payload: { tool: 'inspect_sheets', sheets: 1 }, created_at: '' });
        events.push({ thread_id: 'thread', turn_id: 'turn', seq: 4, kind: 'action_proposal', payload: { proposal: { kind: 'map', title: 'Add a note', spec: { action_id: 'map.template', scope: { kind: 'sheet_rows', sheet_id: sheetId }, params: { template: { text: 'note: {{story}}' } }, output_names: { rendered: 'note' } } } }, created_at: '' });
        events.push({ thread_id: 'thread', turn_id: 'turn', seq: 5, kind: 'answer', payload: { text: 'The council approved the contract. [1](#cite-1)\n\nYou can [Summarize rows](#action/map.summarize), or use this prepared [Add a note](#action-4).', citation_ids: ['source-1'] }, citations: [{ id: 'source-1', label: 'Stories · row 1 · story', source_kind: 'cell', excerpt: 'The council approved the contract.', status: 'current', message: null, target: { kind: 'cell', sheet_id: sheetId, row_id: 1, column_id: 1 } }], created_at: '' });
        events.push({ thread_id: 'thread', turn_id: 'turn', seq: 6, kind: 'result_suggestion', payload: { title: 'Records analyzed in stories', sheet_name: 'stories', total: 1, citation_id: 'query-1' }, created_at: '' });
        events.push({ thread_id: 'thread', turn_id: 'turn', seq: 7, kind: 'status', payload: { status: 'completed' }, created_at: '' });
      }
      return route.fulfill({ json: { events: events.filter((event) => event.seq > Number(url.searchParams.get('after') ?? 0)), cursor: events.length, has_more: false, active_turn: active ? turnWire() : null } });
    }
    if (tail === '/thread/citations/source-1') return route.fulfill({ json: { id: 'source-1', label: 'Stories · row 1 · story', source_kind: 'cell', excerpt: 'The council approved the contract.', status: 'current', message: null, target: { kind: 'cell', sheet_id: sheetId, row_id: 1, column_id: 1 } } });
    if (tail === '/thread/citations/query-1') return route.fulfill({ json: { id: 'query-1', label: 'Council records', source_kind: 'query', excerpt: null, status: 'current', message: null, target: { kind: 'query', sheet_id: sheetId, row_ids: [1, 2], filter: { story: { contains: 'council' } }, sort: null, total: 1 } } });
    if (tail === '/thread') return route.fulfill({ json: { thread, active_turn: active ? turnWire() : null,
      history: { events, cursor: events.length, has_more: false, active_turn: active ? turnWire() : null } } });
    return route.fallback();
  });
  await page.goto(`/p/${pid}`);
  await page.getByTestId(`workbench-mainView-tab-${sheetId}`).click();
  await page.getByTestId('chrome-ask-toggle').click();
  const dock = page.getByTestId('ask-dock');
  await expect(dock).toBeVisible();
  await dock.getByRole('button', { name: 'Add sources', exact: true }).click();
  const sourcePicker = page.getByRole('dialog', { name: 'Choose project sources' });
  await expect(sourcePicker).toBeVisible();
  await expect(sourcePicker.getByRole('checkbox', { name: 'Whole project', exact: true })).not.toBeChecked();
  await sourcePicker.getByRole('button', { name: 'Use sources', exact: true }).click();
  const question = dock.getByRole('textbox', { name: 'Question', exact: true });
  await expect(question).toHaveCSS('resize', 'none');
  const initialHeight = await question.evaluate((element) => element.getBoundingClientRect().height);
  const lineHeight = await question.evaluate((element) => Number.parseFloat(getComputedStyle(element).lineHeight));
  expect(initialHeight).toBeLessThan(lineHeight * 2);
  await page.screenshot({ path: test.info().outputPath('ask-single-line.png'), fullPage: true });
  await question.fill('First line\nSecond line\nThird line');
  await expect.poll(() => question.evaluate((element) => element.getBoundingClientRect().height)).toBeGreaterThan(initialHeight * 2);
  await question.fill('');
  await expect.poll(() => question.evaluate((element) => element.getBoundingClientRect().height)).toBe(initialHeight);
  await question.fill('A question that wraps naturally across several lines in the composer. '.repeat(5));
  await expect.poll(() => question.evaluate((element) => element.getBoundingClientRect().height)).toBeGreaterThan(initialHeight * 2);
  await page.screenshot({ path: test.info().outputPath('ask-growing-input.png'), fullPage: true });
  await question.fill('What changed?');
  await dock.getByRole('button', { name: 'Collapse Ask' }).click();
  await expect(dock).not.toBeVisible();
  await page.getByTestId('chrome-ask-toggle').click();
  await expect(dock.getByRole('textbox', { name: 'Question', exact: true })).toHaveValue('What changed?');
  await dock.getByRole('button', { name: 'Send', exact: true }).click();
  await expect(dock.getByRole('button', { name: 'Stop', exact: true })).toBeVisible();
  const working = dock.locator('details').filter({ has: page.getByText('Working', { exact: true }) });
  await expect(working).toHaveAttribute('open', '');
  await expect(question).toHaveValue('');
  await expect.poll(() => question.evaluate((element) => element.getBoundingClientRect().height)).toBe(initialHeight);
  await expect(dock.locator('.ask-answer')).toContainText('The council approved the contract.');
  await expect(dock.getByRole('button', { name: 'Stop', exact: true })).not.toBeVisible();
  await expect(working).not.toHaveAttribute('open');
  await working.getByText('Working', { exact: true }).click();
  await expect(dock.getByText('Checking sources', { exact: true })).toHaveCount(1);
  await working.getByText('Working', { exact: true }).click();
  await page.reload();
  if (!await dock.isVisible()) await page.getByTestId('chrome-ask-toggle').click();
  await expect(dock.locator('.ask-answer')).toContainText('The council approved the contract.');
  await expect(dock.locator('.ask-answer')).toHaveCSS('padding-right', '0px');
  await expect(dock.getByRole('button', { name: 'Copy answer text', exact: true }).locator('..')).toHaveCSS('position', 'absolute');
  await dock.getByRole('button', { name: /^Source 1/ }).click();
  await expect(page.getByTestId('row-drawer')).toBeVisible();
  await expect(page.getByTestId('sheet-stats')).toContainText('2 rows');
  await expect(page.getByRole('button', { name: 'Back to previous view', exact: true })).toHaveCount(0);
  await expect(page.getByLabel('Source opened from Ask', { exact: true })).toHaveCount(0);
  await dock.getByRole('button', { name: '1 row in stories', exact: true }).click();
  await expect(page.getByTestId('sheet-stats')).toContainText('1 row');
  await page.getByTestId(`workbench-mainView-tab-${otherSheetId}`).click();
  await expect(dock.getByText('Viewing council audio', { exact: true })).toBeVisible();
  await expect(dock.getByRole('button', { name: 'Remove stories', exact: true })).toBeVisible();
  await page.screenshot({ path: test.info().outputPath('ask-context.png'), fullPage: true });
  await dock.getByRole('button', { name: 'Use this sheet', exact: true }).click();
  await expect(dock.getByRole('button', { name: 'Remove council audio', exact: true })).toBeVisible();
  await expect(dock.getByText('Viewing council audio', { exact: true })).toHaveCount(0);
  await dock.getByRole('button', { name: 'Remove council audio', exact: true }).click();
  await expect(dock.getByText('Entire project', { exact: true })).toBeVisible();
  await dock.getByRole('button', { name: 'Ask options', exact: true }).click();
  const settings = page.getByRole('dialog', { name: 'Ask settings' });
  await expect(settings).toBeVisible();
  await expect(settings.getByRole('checkbox', { name: 'Web', exact: true })).toBeVisible();
  await expect(settings.getByText('Test model', { exact: true })).toBeVisible();
  const settingsBounds = await settings.boundingBox();
  expect(settingsBounds).not.toBeNull();
  expect(settingsBounds!.y + settingsBounds!.height).toBeLessThanOrEqual(page.viewportSize()!.height);
  await page.screenshot({ path: test.info().outputPath('ask-options.png'), fullPage: true });
  await page.keyboard.press('Escape');
  await expect(settings).not.toBeVisible();
  await dock.getByTestId('ask-thread-menu-trigger').click();
  await expect(page.getByRole('menuitemradio', { name: 'What changed?', exact: true })).toBeVisible();
  await page.keyboard.press('Escape');
  await dock.getByRole('button', { name: 'Summarize rows', exact: true }).click();
  await expect(page.getByTestId('action-form-title')).toContainText('Summarize');
  await dock.getByRole('button', { name: 'Add a note', exact: true }).click();
  await expect(page.getByTestId('action-panel')).toBeVisible();
  await expect(page.getByTestId('action-form-title')).toContainText('Add a note');
  await expect(dock).toBeVisible();
  await page.screenshot({ path: test.info().outputPath('ask-docked.png'), fullPage: true });
  await page.evaluate(() => { document.documentElement.dataset.frisketTheme = 'dark'; });
  await page.screenshot({ path: test.info().outputPath('ask-dark.png'), fullPage: true });
});


test('sending a follow-up scrolls to it even after reading older messages', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-ask-scroll'));
  await importCsv(page.request, pid, 'notes.csv', 'note\nA council decision.\n');
  await stubSelectorChoices(page, pid, (subject) => selectorChoicesResponse({
    projectId: pid, subject, currentChoiceId: 'test-model', groups: [{
      id: 'local', label: 'On this computer', choices: [{ choiceId: 'test-model', label: 'Test model', authoredSelection: { kind: 'model', model: 'ollama/test' } }],
    }],
  }));
  const thread: AskThread = { id: 'history', title: 'Earlier research', scope: { kind: 'project' }, model: 'ollama/test', web: false, suggest_actions: true, revision: 1, created_by: null, created_at: '', updated_at: '' };
  const events: AskEvent[] = Array.from({ length: 35 }, (_, i) => ({ thread_id: 'history', turn_id: 'old', seq: i + 1, kind: 'answer', payload: { text: `Earlier finding ${i + 1}. The council postponed the decision until the next meeting.`, citation_ids: [] }, created_at: '' }));
  let active: AskTurn | null = null;
  await page.route(`**/api/projects/${pid}/qa/threads**`, async (route) => {
    const tail = new URL(route.request().url()).pathname.split('/qa/threads')[1];
    if (!tail) return route.fulfill({ json: [thread] });
    if (tail === '/history') return route.fulfill({ json: { thread, active_turn: active, history: { events, cursor: events.length, has_more: false, active_turn: active } } });
    if (tail === '/history/turns') {
      const body = route.request().postDataJSON();
      events.push({ thread_id: 'history', turn_id: 'new', seq: events.length + 1, kind: 'question', payload: { question: body.question }, created_at: '' });
      active = { id: 'new', thread_id: 'history', request_id: body.request_id, question: body.question, status: 'running', submitted_by: null, started_at: '', finished_at: null, usage: null, cost_actual: null, error_summary: null, scope: thread.scope, model: thread.model, web: false, suggest_actions: true };
      return route.fulfill({ json: active });
    }
    if (tail === '/history/events') return route.fulfill({ json: { events, cursor: events.length, has_more: false, active_turn: active } });
    return route.fallback();
  });
  await page.goto(`/p/${pid}`);
  await page.getByTestId('chrome-ask-toggle').click();
  const dock = page.getByTestId('ask-dock');
  await expect(dock.getByText('Earlier finding 1.', { exact: false })).toBeVisible();
  await dock.locator('.ask-history').evaluate((element) => { element.scrollTop = 0; });
  await dock.getByRole('textbox', { name: 'Question', exact: true }).fill('What happened next?');
  await dock.getByRole('button', { name: 'Send', exact: true }).click();
  await expect(dock.getByText('What happened next?', { exact: true })).toBeInViewport();
});
