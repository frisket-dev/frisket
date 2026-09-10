// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { EditionModuleProvider } from '../../src/editions/module';
import { LOCAL_EDITION_MODULE } from '../../src/editions/openModules';
import { WalkthroughContext } from '../../src/walkthrough/context';
import { servedActionCatalog } from '../support/servedActionCatalog';

vi.mock('../../src/media/pdfjsSetup', () => ({
  pdfjsLib: { getDocument: () => {}, TextLayer: class {} },
}));
import { ProjectRoute } from '../../src/App';

const project = { id: 'empty-authoring', name: 'Empty authoring' };
const catalog = servedActionCatalog();
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), {
  status, headers: { 'Content-Type': 'application/json' },
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

function launch(actionKind: string, previews: unknown[]) {
  window.history.replaceState({}, '', `/p/${project.id}/action/${actionKind}`);
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === '/api/projects') return json([project]);
    if (url.includes('/catalog')) return json(catalog);
    if (url.endsWith('/validate-params')) return json({ diagnostics: {},
      logical_outputs: [{ key: 'Name', column_type: 'text' }], creates_sheet: true });
    if (url.endsWith('/actions/v1/preview') && init?.method === 'POST') {
      previews.push(JSON.parse(String(init.body)));
      return json({ schema_version: 'frisket.action_preview.v1', preview_id: 'sample', total: null }, 202);
    }
    if (url.endsWith('/actions/v1/preview/sample')) return json({
      schema_version: 'frisket.action_preview.v1', preview_id: 'sample', status: 'done',
      progress: { done: 1, total: null }, result: { kind: 'table', columns: [{
        name: 'Name', column_type: 'text', format: null, hidden: false, overwrites_column_id: null,
      }], rows: [{ Name: { value: 'Ada' } }], sampled: 1, total: null },
    });
    if (url.endsWith('/sheets') || url.endsWith('/watches') || url.endsWith('/saved-actions')) return json([]);
    return json({});
  }));
  return render(<EditionModuleProvider edition={LOCAL_EDITION_MODULE}>
    <WalkthroughContext.Provider value={{ active: false, canResume: false, guideSeen: true,
      openWalkthroughChooser: vi.fn(), resumeWalkthrough: vi.fn(), startWalkthrough: vi.fn(), stopWalkthrough: vi.fn() }}>
      <ProjectRoute projectId={project.id} actionKind={actionKind} />
    </WalkthroughContext.Provider>
  </EditionModuleProvider>);
}

it('opens a project action from the real empty-workspace route and previews a canonical table request', async () => {
  const previews: unknown[] = [];
  launch('import.rows', previews);
  await screen.findByTestId('generated-action-form', {}, { timeout: 5000 });
  fireEvent.change(screen.getByTestId('field-columns'), {
    target: { value: JSON.stringify([{ name: 'Name', type: 'text' }]) },
  });
  fireEvent.change(screen.getByTestId('field-rows'), { target: { value: JSON.stringify([{ Name: 'Ada' }]) } });
  fireEvent.change(screen.getByTestId('field-sheet_name'), { target: { value: 'People' } });
  await waitFor(() => expect(screen.getByTestId('generated-action-preview')).toBeEnabled());
  fireEvent.click(screen.getByTestId('generated-action-preview'));
  await screen.findByRole('table', { name: 'Preview sample' }, { timeout: 5_000 });
  expect(previews).toEqual([{ action_id: 'import.rows', scope: { kind: 'project' },
    params: { columns: [{ name: 'Name', type: 'text' }], rows: [{ Name: 'Ada' }], source: null },
    sheet_name: 'People', output_names: {}, idempotency_key: expect.any(String) }]);
  expect(screen.getByRole('table', { name: 'Preview sample' })).toHaveTextContent('Ada');
});

it('refuses a row action in the same empty workspace before resolving or submitting it', async () => {
  const previews: unknown[] = [];
  launch('map.ask', previews);
  expect(await screen.findByText('Choose a sheet before opening this action.')).toBeInTheDocument();
  expect(screen.queryByTestId('generated-action-form')).not.toBeInTheDocument();
  expect(previews).toEqual([]);
  expect(vi.mocked(fetch).mock.calls.some(([url]) => String(url).endsWith('/validate-params'))).toBe(false);
});
