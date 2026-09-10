// @vitest-environment jsdom
//
// SourceInputControl is shared by the typed extract / translate / classify
// forms through GeneratedActionForm's rich-source control. This file pins
// that the typed Extract form renders the shared three-mode control and
// serializes exactly what the served map.extract Params schema admits: a
// column-name array in Column(s) mode, `{ text }` in Template mode.

import '@testing-library/jest-dom/vitest';
import { act, cleanup, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../../src/api/open';
import type { SheetMeta } from '../../src/api/types';
import { columnDef } from '../support/domainFixtures';
import { sheetMeta } from '../support/actionFormFixtures';
import { installPopoverPolyfill } from '../support/domPolyfills';
import { mountTypedForm, servedTypedEntry, type TypedResolveParams } from './typedFormCutoverF2';

beforeAll(installPopoverPolyfill);

beforeEach(() => {
  // Extract requires a model; the provider catalog supplies the default.
  vi.spyOn(api, 'listProviders').mockResolvedValue({
    schemaVersion: 'frisket.providers.v1',
    tier: 'local',
    providers: [{ id: 'test', label: 'Test', kind: 'platform_api', configured: true, source: 'env',
      hint: null, models: [{ id: 'test/model', label: 'Test model', price: null }] }],
  });
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('No provider request in this test')));
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function textSheet(): SheetMeta {
  return sheetMeta([
    columnDef({ id: '1', name: 'headline', type: 'text' }),
    columnDef({ id: '2', name: 'body', type: 'text' }),
  ], { id: '7' });
}

/** Every extract field is one logical output keyed by its name. */
const resolveExtract: TypedResolveParams = async ({ params }) => ({
  diagnostics: {},
  logical_outputs: (Array.isArray(params.fields) ? params.fields as Array<{ name: string; type: string }> : [])
    .map((field) => ({ key: field.name, column_type: field.type })),
});

function mountExtract() {
  return mountTypedForm({
    entry: servedTypedEntry('map.extract'),
    sheet: textSheet(),
    resolveParams: resolveExtract,
  });
}

/** Run once the debounced server resolution admits the draft. The typed form
 *  has no client-side cost gate — the point of these tests is the SOURCE
 *  serialization. */
async function submitRun(user: ReturnType<typeof userEvent.setup>): Promise<void> {
  await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
  await user.click(screen.getByTestId('generated-action-run'));
}

describe('shared rich-source control', () => {
  it('extract offers All, Column(s) and { } Template through the shared control', () => {
    mountExtract();

    expect(screen.getByTestId('text-source-mode-all')).toBeVisible();
    expect(screen.getByTestId('text-source-mode-columns')).toBeVisible();
    expect(screen.getByTestId('text-source-mode-template')).toBeVisible();
    // The chip picker, not the legacy single-column <select> or two-mode toggle.
    expect(screen.getByTestId('text-source-columns')).toBeVisible();
    expect(screen.queryByTestId('text-source-column-select')).not.toBeInTheDocument();
    expect(screen.queryByTestId('text-source-mode-column')).not.toBeInTheDocument();
  });

  it('serializes a column-name array in Column(s) mode', async () => {
    const user = userEvent.setup();
    const { onExecute } = mountExtract();

    expect(screen.getByTestId('text-source-mode-columns')).toHaveAttribute('aria-pressed', 'true');
    expect(within(screen.getByTestId('text-source-columns')).getByText('headline')).toBeVisible();
    await submitRun(user);

    expect(onExecute).toHaveBeenCalledTimes(1);
    const request = onExecute.mock.calls[0][0];
    expect(request.action_id).toBe('map.extract');
    expect(request.params.source).toEqual(['headline']);
    expect(request.params).not.toHaveProperty('input_template');
    expect(request.params).not.toHaveProperty('input_columns');
  });

  it('serializes { text } in Template mode', async () => {
    const user = userEvent.setup();
    const { onExecute } = mountExtract();

    await user.click(screen.getByTestId('text-source-mode-template'));
    await user.selectOptions(screen.getByTestId('text-source-template-column-insert'), 'body');
    // The composer restores the caret via requestAnimationFrame after a token
    // insert (returning focus to itself). Let that settle before submitting,
    // so the deferred refocus can't interleave with the click.
    await act(async () => {
      await new Promise(requestAnimationFrame);
    });
    await submitRun(user);

    expect(onExecute).toHaveBeenCalledTimes(1);
    const request = onExecute.mock.calls[0][0];
    expect(request.params.source).toEqual({ text: '{{body}}' });
    expect(request.params).not.toHaveProperty('input_columns');
  });
});
