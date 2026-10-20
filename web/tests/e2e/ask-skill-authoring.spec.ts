import { expect, test } from '@playwright/test';
import { createProject, importCsv, uniqueName } from './helpers';
import { selectorChoicesResponse, stubSelectorChoices } from './selectorChoicesFixture';

// The real provider/store flow is covered by test_project_qa_skill_authoring_flow.py.
// This browser contract covers the roomy editor, repeated Save and same-chat return.
test('review a long skill, save twice, and reopen its saved text', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-ask-skill'));
  const sheetId = await importCsv(page.request, pid, 'suppliers.csv', 'supplier,amount\nAlpha,10\nBeta,900\n');
  await stubSelectorChoices(page, pid, (subject) => selectorChoicesResponse({
    projectId: pid, subject, currentChoiceId: 'test-model', groups: [{
      id: 'local', label: 'On this computer', choices: [{ choiceId: 'test-model', label: 'Test model',
        authoredSelection: { kind: 'model', model: 'ollama/test' } }],
    }],
  }));
  const content = '---\nname: check-supplier\ndescription: Check the selected supplier and explain the evidence.\n---\n\n'
    + '# Check the selected supplier\n\n'
    + Array.from({ length: 45 }, (_, n) => `${n + 1}. Read the admitted rows. Keep stable row IDs and cite the source. Do not broaden the scope without asking.\n`).join('');
  const scope = { kind: 'sources', sources: [{ kind: 'sheet', sheet_id: sheetId }] };
  const thread = { id: 'thread', title: 'Supplier workflow', scope, model: 'ollama/test', web: false,
    suggest_actions: true, revision: 1, created_by: null, created_at: '', updated_at: '' };
  const events: Array<{ thread_id: string; turn_id: string; seq: number; kind: string; payload: object; created_at: string }> = [];
  const add = (kind: string, payload: object) => events.push({ thread_id: 'thread', turn_id: 'turn', seq: events.length + 1, kind, payload, created_at: '' });
  add('question', { question: 'Make a skill for checking the selected supplier.' });
  add('skill_proposal', { proposal_id: 'proposal', target_skill_id: null, base_revision: null,
    name: 'check-supplier', description: 'Check the selected supplier and explain the evidence.', content,
    enabled: true, scope: 'workspace' });
  add('answer', { text: 'Review the text before saving the skill.', citation_ids: [] });
  add('status', { status: 'completed' });
  let revision = 0;
  const savedBodies: string[] = [];
  const history = () => ({ events, cursor: events.length, has_more: false, active_turn: null });
  await page.route(`**/api/projects/${pid}/qa/threads**`, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const tail = url.pathname.split('/qa/threads')[1];
    if (!tail && request.method() === 'GET') return route.fulfill({ json: [thread] });
    if (tail === '/thread') return route.fulfill({ json: { thread, active_turn: null, history: history(), can_manage_skills: true, skill_scope: 'workspace' } });
    if (tail === '/thread/events') return route.fulfill({ json: { ...history(), events: events.filter((event) => event.seq > Number(url.searchParams.get('after') ?? 0)) } });
    if (tail === '/thread/turns/turn/skill-proposals/proposal/save') {
      const body = request.postDataJSON();
      expect(body.expected_revision).toBe(revision || null);
      expect(body.request_id).toBeTruthy();
      savedBodies.push(body.content);
      revision += 1;
      const attemptId = `attempt-${revision}`;
      add('skill_save_attempt', { attempt_id: attemptId, proposal_id: 'proposal', request_id: body.request_id, name: 'check-supplier', description: 'Check the selected supplier and explain the evidence.', content: body.content, expected_revision: body.expected_revision });
      add('skill_saved', { attempt_id: attemptId, proposal_id: 'proposal', skill_id: 'skill', name: 'check-supplier', description: 'Check the selected supplier and explain the evidence.', enabled: true, revision, status: 'saved' });
      return route.fulfill({ json: { status: 'saved', attempt_id: attemptId, proposal_id: 'proposal', skill: { id: 'skill', name: 'check-supplier', description: 'Check the selected supplier and explain the evidence.', content: body.content, enabled: true, revision, createdAt: '', updatedAt: '' } } });
    }
    return route.fallback();
  });
  await page.goto(`/p/${pid}`);
  await page.getByTestId('chrome-ask-toggle').click();
  const dock = page.getByTestId('ask-dock');
  await dock.getByRole('button', { name: 'Review the text', exact: true }).click();
  const dialog = page.getByTestId('ask-skill-review-dialog');
  const editor = dialog.getByTestId('ask-skill-editor');
  await expect(editor).toHaveValue(content);
  const bounds = await dialog.boundingBox();
  expect(bounds!.width).toBeGreaterThan(700);
  expect(bounds!.height).toBeLessThanOrEqual(900);
  await page.screenshot({ path: test.info().outputPath('skill-review-long-light.png'), fullPage: true });
  await editor.fill(content + '\nOnly in the editor: first revision.\n');
  await dialog.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(dialog.getByRole('status')).toContainText('Saved');
  await expect(dialog).toBeVisible();
  await editor.fill(content + '\nOnly in the editor: second revision.\n');
  await dialog.getByRole('button', { name: 'Save', exact: true }).click();
  await expect.poll(() => savedBodies.length).toBe(2);
  await expect(dialog.getByRole('status')).toContainText('Saved');
  await dialog.getByRole('button', { name: 'Close', exact: true }).last().click();
  await expect(dialog).not.toBeVisible();
  await expect(dock).toBeVisible();
  await expect(dock.getByText('Use it now', { exact: true })).toHaveCount(0);
  await dock.getByRole('button', { name: 'Review the text', exact: true }).click();
  await expect(editor).toHaveValue(savedBodies[1]);
  await page.setViewportSize({ width: 800, height: 700 });
  await page.screenshot({ path: test.info().outputPath('skill-review-narrow.png'), fullPage: true });
  expect(await dialog.evaluate((element) => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
  await editor.fill(savedBodies[1] + '\nUnsaved change.');
  await dialog.getByRole('button', { name: 'Close', exact: true }).last().click();
  await expect(dialog.getByRole('alertdialog')).toBeVisible();
  await dialog.getByRole('button', { name: 'Keep editing', exact: true }).click();
  await expect(editor).toHaveValue(savedBodies[1] + '\nUnsaved change.');
});
