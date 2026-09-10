// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { useState } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { isGeneratedActionCatalogEntry, type GeneratedActionCatalogEntry } from '../../src/api/types';
import { actionTemplatesFromCatalog, generatedActionTemplateFromCatalogEntry } from '../../src/actions/model';
import { GeneratedActionForm } from '../../src/components/action-panel/GeneratedActionForm';
import { deriveActionPresentationCatalog } from '../../src/components/action-panel/actionPresentation';
import type { GeneratedActionFieldProps } from '../../src/components/action-panel/generatedActionCustomizations';
import type { DynamicGeneratedActionParamsBody } from '../../src/components/action-panel/GeneratedActionParamsBody';
import { loadTrustedLocalPluginExport } from '../../src/workbench/trustedLocalModule';
import { syntheticActionCatalogEntry } from '../support/actionCatalogFixtures';
import { sheetMeta, completeCatalogPayload } from '../support/actionFormFixtures';
import { columnDef } from '../support/domainFixtures';

vi.mock('../../src/workbench/trustedLocalModule', () => ({ loadTrustedLocalPluginExport: vi.fn() }));
afterEach(() => { cleanup(); vi.resetAllMocks(); });

function entry(): GeneratedActionCatalogEntry {
  return syntheticActionCatalogEntry('acme.names.clean', {
    input_schema: { type: 'object', required: ['source'], properties: {
      source: { type: 'string' }, prefix: { type: 'string', default: '' },
    } },
    row_scope_policy: { kind: 'sheet_rows', selectors: ['all_rows', 'exact_membership'] },
    ui_hints: { form: 'CleanUI', semantic_controls: { source: 'column' },
      logical_outputs: [{ key: 'text', column_type: 'text' }],
      action_ui: { plugin_id: 'acme.names', export_name: 'CleanUI', package_sha256: `sha256:${'a'.repeat(64)}` } },
  }) as GeneratedActionCatalogEntry;
}

function Prefix({ value, onChange, id }: GeneratedActionFieldProps) {
  const [count, setCount] = useState(0);
  return <label htmlFor={id}>Custom prefix<input id={id} value={String(value ?? '')}
    onChange={event => { setCount(count + 1); onChange(event.target.value); }} />
    <span data-testid="edits">{count}</span></label>;
}
const Body: DynamicGeneratedActionParamsBody = ({ Field }) => <section data-testid="custom-body">
  <Field name="source" /><Field name="prefix" />
</section>;

function props(catalogEntry = entry()) {
  return { catalogEntry, projectId: 'project-one',
    actionTemplate: generatedActionTemplateFromCatalogEntry(catalogEntry)!,
    sheet: sheetMeta([columnDef({ id: '1', name: 'Original', type: 'text' })], { id: '7', rowCount: 2 }),
    running: false, onClose: vi.fn(), onExecute: vi.fn(),
    resolveParams: vi.fn(async () => ({ diagnostics: {}, logical_outputs: [{ key: 'text', column_type: 'text' }] })),
  };
}

it.each([false, true])('mounts field controls as React components, body=%s, and submits canonical requests', async body => {
  vi.mocked(loadTrustedLocalPluginExport).mockResolvedValue(() => ({ fields: { prefix: Prefix }, ...(body ? { body: Body } : {}) }));
  const input = props();
  render(<GeneratedActionForm {...input} />);
  fireEvent.change(await screen.findByLabelText(/Custom prefix/), { target: { value: 'Dr ' } });
  expect(screen.getByTestId('edits')).toHaveTextContent('1');
  if (body) expect(screen.getByTestId('custom-body')).toBeVisible();
  const preview = screen.getByRole('button', { name: /Preview/ });
  await waitFor(() => expect(preview).toBeEnabled());
  fireEvent.click(preview);
  expect(input.onExecute).toHaveBeenCalledWith(expect.objectContaining({ action_id: 'acme.names.clean',
    params: { source: 'Original', prefix: 'Dr ' }, scope: { kind: 'sheet_rows', sheet_id: 7 },
    output_names: { text: 'text' }, idempotency_key: expect.any(String) }), 'preview');
  expect(loadTrustedLocalPluginExport).toHaveBeenCalledWith(
    `/api/projects/project-one/workbench/plugins/acme.names/frontend-components/acme.names.clean/module.js?package=sha256%3A${'a'.repeat(64)}`,
    'CleanUI', { exact: true });
});

it.each([() => ({ fields: { typo: Prefix } }), () => ({ body: 'wrong' }), () => ({ run: () => null }), () => new Date()])(
  'blocks malformed UI before mounting the form', async factory => {
    vi.mocked(loadTrustedLocalPluginExport).mockResolvedValue(factory);
    const input = props(); render(<GeneratedActionForm {...input} />);
    expect(await screen.findByRole('alert')).toHaveTextContent('could not load');
    expect(input.resolveParams).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: /Preview/ })).not.toBeInTheDocument();
  });

it('unmounts old state immediately when the project/package identity changes', async () => {
  vi.mocked(loadTrustedLocalPluginExport).mockResolvedValue(() => ({ fields: { prefix: Prefix } }));
  const input = props(); const view = render(<GeneratedActionForm {...input} />);
  fireEvent.change(await screen.findByLabelText(/Custom prefix/), { target: { value: 'old' } });
  vi.mocked(loadTrustedLocalPluginExport).mockReturnValue(new Promise(() => {}));
  view.rerender(<GeneratedActionForm {...input} projectId="project-two" />);
  expect(screen.queryByLabelText(/Custom prefix/)).not.toBeInTheDocument();
  expect(screen.getByRole('status')).toHaveTextContent('Loading');
});

it('uses one generated-shell predicate and refuses an unbound form string', () => {
  const valid = entry(); const template = generatedActionTemplateFromCatalogEntry(valid)!;
  expect(isGeneratedActionCatalogEntry(valid)).toBe(true);
  expect(template.generatedAction).toBe(true);
  const catalog = completeCatalogPayload([valid]);
  expect(deriveActionPresentationCatalog(catalog, actionTemplatesFromCatalog(catalog)).get(valid.kind)).toEqual({ kind: 'generated' });
  const unbound = { ...valid, ui_hints: { ...valid.ui_hints, action_ui: undefined } };
  expect(isGeneratedActionCatalogEntry(unbound)).toBe(false);
  expect(generatedActionTemplateFromCatalogEntry(unbound)).toBeNull();
});

it('does not invoke a stale factory after switching projects', async () => {
  let resolveOld!: (value: unknown) => void;
  const oldFactory = vi.fn(() => ({ fields: { prefix: Prefix } }));
  vi.mocked(loadTrustedLocalPluginExport).mockReturnValueOnce(new Promise(resolve => { resolveOld = resolve; }))
    .mockResolvedValueOnce(() => ({ fields: { prefix: Prefix } }));
  const input = props(); const view = render(<GeneratedActionForm {...input} />);
  view.rerender(<GeneratedActionForm {...input} projectId="project-two" />);
  await screen.findByLabelText(/Custom prefix/);
  await act(async () => { resolveOld(oldFactory); });
  expect(oldFactory).not.toHaveBeenCalled();
});
