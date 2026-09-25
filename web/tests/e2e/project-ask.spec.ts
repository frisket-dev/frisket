import { expect, test } from '@playwright/test';
import { createProject, importCsv, uniqueName } from './helpers';
import { selectorChoicesResponse, stubSelectorChoices } from './selectorChoicesFixture';

test('Ask stays docked, keeps its draft, and reconnects to saved progress', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-project-ask'));
  const sheetId = await importCsv(page.request, pid, 'stories.csv', 'story\nThe council approved the contract.\nThe school opened.\n');
  await stubSelectorChoices(page, pid, (subject) => selectorChoicesResponse({
    projectId: pid, subject, currentChoiceId: 'test-model', groups: [{
      id: 'local', label: 'On this computer', choices: [{ choiceId: 'test-model', label: 'Test model',
        authoredSelection: { kind: 'model', model: 'ollama/test' } }],
    }],
  }));
  const thread = { id: 'thread', title: 'What changed?', scope: { kind: 'sources', sources: [{ kind: 'sheet', sheet_id: sheetId }] },
    model: 'ollama/test', web: false, suggest_actions: true, revision: 1, created_by: null, created_at: '', updated_at: '' };
  let exists = false;
  let active = false;
  let polls = 0;
  const events: { thread_id: string; turn_id: string; seq: number; kind: string; payload: object; created_at: string }[] = [];
  const turn = { ...thread, id: 'turn', thread_id: thread.id, request_id: 'request', question: 'What changed?',
    status: 'running', submitted_by: null, started_at: '', finished_at: null, usage: null, cost_actual: null, error_summary: null };
  const turnWire = () => {
    const { title: _title, revision: _revision, created_by: _creator, created_at: _created, updated_at: _updated, ...wire } = turn;
    return wire;
  };
  await page.route(`**/api/projects/${pid}/qa/threads**`, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const tail = url.pathname.split('/qa/threads')[1];
    if (!tail && request.method() === 'GET') return route.fulfill({ json: exists ? [thread] : [] });
    if (!tail && request.method() === 'POST') { exists = true; return route.fulfill({ json: thread }); }
    if (tail === '/thread/turns') {
      const body = request.postDataJSON();
      expect(body.scope).toEqual(thread.scope);
      expect(body.messages).toBeUndefined();
      expect(body.question).toBe('What changed?');
      active = true;
      events.push({ thread_id: 'thread', turn_id: 'turn', seq: 1, kind: 'question', payload: { question: body.question }, created_at: '' });
      return route.fulfill({ json: turnWire() });
    }
    if (tail === '/thread/events') {
      if (active && ++polls >= 2) {
        active = false;
        events.push({ thread_id: 'thread', turn_id: 'turn', seq: 2, kind: 'answer', payload: { text: 'The council approved the contract.', citation_ids: ['source-1'] }, created_at: '' });
        events.push({ thread_id: 'thread', turn_id: 'turn', seq: 3, kind: 'action_proposal', payload: { proposal: { kind: 'map', title: 'Add a note', spec: { action_id: 'map.template', scope: { kind: 'sheet_rows', sheet_id: sheetId }, params: { template: { text: 'note: {{story}}' } }, output_names: { rendered: 'note' } } } }, created_at: '' });
        events.push({ thread_id: 'thread', turn_id: 'turn', seq: 4, kind: 'status', payload: { status: 'completed' }, created_at: '' });
      }
      return route.fulfill({ json: { events: events.filter((event) => event.seq > Number(url.searchParams.get('after') ?? 0)), cursor: events.length, has_more: false, active_turn: active ? turnWire() : null } });
    }
    if (tail === '/thread/citations/source-1') return route.fulfill({ json: { id: 'source-1', label: 'Stories · row 1 · story', source_kind: 'cell', excerpt: 'The council approved the contract.', status: 'current', message: null, target: { kind: 'cell', sheet_id: sheetId, row_id: 1, column_id: 1 } } });
    if (tail === '/thread') return route.fulfill({ json: { thread, active_turn: active ? turnWire() : null,
      history: { events, cursor: events.length, has_more: false, active_turn: active ? turnWire() : null } } });
    return route.fallback();
  });
  await page.goto(`/p/${pid}`);
  await page.getByTestId('chrome-copilot-toggle').click();
  const dock = page.getByTestId('ask-dock');
  await expect(dock).toBeVisible();
  await dock.getByRole('button', { name: 'Add sources', exact: true }).click();
  const sourcePicker = page.getByRole('dialog', { name: 'Choose project sources' });
  await expect(sourcePicker).toBeVisible();
  await expect(sourcePicker.getByRole('checkbox', { name: 'Whole project', exact: true })).not.toBeChecked();
  await sourcePicker.getByRole('button', { name: 'Use sources', exact: true }).click();
  await dock.getByRole('textbox', { name: 'Question', exact: true }).fill('What changed?');
  await dock.getByRole('button', { name: 'Collapse Ask' }).click();
  await expect(dock).not.toBeVisible();
  await page.getByTestId('chrome-copilot-toggle').click();
  await expect(dock.getByRole('textbox', { name: 'Question', exact: true })).toHaveValue('What changed?');
  await dock.getByRole('button', { name: 'Send', exact: true }).click();
  await expect(dock.getByRole('button', { name: 'Stop', exact: true })).toBeVisible();
  await expect(dock.getByText('The council approved the contract.', { exact: true })).toBeVisible();
  await expect(dock.getByRole('button', { name: 'Stop', exact: true })).not.toBeVisible();
  await page.reload();
  if (!await dock.isVisible()) await page.getByTestId('chrome-copilot-toggle').click();
  await expect(dock.getByText('The council approved the contract.', { exact: true })).toBeVisible();
  await dock.getByRole('button', { name: 'Source 1', exact: true }).click();
  await expect(dock.getByText('Stories · row 1 · story', { exact: true })).toBeVisible();
  await dock.getByRole('button', { name: 'Open action', exact: true }).click();
  await expect(page.getByTestId('action-panel')).toBeVisible();
  await expect(page.getByTestId('action-form-title')).toContainText('Add a note');
  await expect(dock).toBeVisible();
  await page.screenshot({ path: test.info().outputPath('ask-docked.png'), fullPage: true });
});
