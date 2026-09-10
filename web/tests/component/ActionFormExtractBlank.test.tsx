// @vitest-environment jsdom
//
// A fresh typed Extract form starts with a blank, unplaceholdered instruction
// and exactly ONE seeded output column (`value`, text, no description) so the
// request is valid without inventing example prose; further columns appear
// only when the user asks, and a saved instruction + schema reopen intact.

import '@testing-library/jest-dom/vitest';
import { cleanup, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { columnDef } from '../support/domainFixtures';
import { sheetMeta } from '../support/actionFormFixtures';
import { lastExecutedRequest, mockLocalProviders, mountTypedForm, pressRun } from './cutoverUiF1TypedForm';

let dispose: (() => void) | undefined;
afterEach(() => {
  cleanup();
  dispose?.();
  dispose = undefined;
  vi.restoreAllMocks();
});

beforeEach(() => {
  mockLocalProviders();
});

const sheet = () => sheetMeta([
  columnDef({ id: '1', name: 'story', type: 'text' }),
], { id: '7' });

function mountExtract(options: Partial<Parameters<typeof mountTypedForm>[0]> = {}) {
  const mounted = mountTypedForm({ kind: 'map.extract', sheet: sheet(), ...options });
  dispose = mounted.dispose;
  return mounted;
}

describe('fresh Extract form', () => {
  it('starts with a blank instruction and one unnamed-by-example output column', async () => {
    const { onExecute } = mountExtract();

    const instruction = screen.getByTestId('field-instruction');
    expect(instruction.tagName).toBe('TEXTAREA');
    expect(instruction).toHaveValue('');
    expect(instruction).not.toHaveAttribute('placeholder');
    expect(screen.getAllByTestId('output-field-row')).toHaveLength(1);
    expect(screen.getByTestId('output-field-name')).toHaveValue('value');
    expect(screen.getByTestId('output-field-description')).toHaveValue('');
    // The legacy form's blank-state helper copy (`extract-fields-hint` "Add one
    // column…", `extract-prompt-hint` "…officials named…") no longer exists on
    // the typed form: ExtractParamsBody / StructuredFieldsEditor render no
    // guidance text for a blank instruction or field set (product change), so
    // there is nothing to assert in its place — the blank-and-unplaceholdered
    // checks above are the whole "no invented example prose" contract now.

    // Nothing invented on the wire: the seeded column and a blank instruction.
    await pressRun();
    const request = lastExecutedRequest(onExecute);
    expect(request.action_id).toBe('map.extract');
    expect(request.params).toMatchObject({
      source: ['story'],
      instruction: '',
      fields: [{ name: 'value', type: 'text', description: '' }],
    });
    expect(request.output_names).toEqual({ value: 'value' });
  });

  it('adds a further column only after the user asks for one', async () => {
    const user = userEvent.setup();
    mountExtract();

    expect(screen.getAllByTestId('output-field-row')).toHaveLength(1);
    await user.click(screen.getByRole('button', { name: 'Add column' }));

    expect(screen.getAllByTestId('output-field-row')).toHaveLength(2);
    expect(screen.getAllByTestId('output-field-name')[1]).toHaveValue('field_2');
  });

  it('preserves a saved instruction and output schema when reopened', () => {
    mountExtract({
      initialDraft: {
        action_id: 'map.extract',
        scope: { kind: 'sheet_rows', sheet_id: 7 },
        params: {
          source: ['story'],
          model: 'test/model',
          instruction: 'Extract the contract identifier.',
          fields: [{
            name: 'contract_id',
            type: 'text',
            description: 'Identifier printed in the filing',
          }],
        },
        output_names: { contract_id: 'contract_id' },
      },
    });

    expect(screen.getByTestId('field-instruction')).toHaveValue('Extract the contract identifier.');
    expect(screen.getByTestId('output-field-name')).toHaveValue('contract_id');
    expect(screen.getByTestId('output-field-description')).toHaveValue(
      'Identifier printed in the filing',
    );
  });
});
