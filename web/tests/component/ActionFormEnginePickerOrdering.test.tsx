// @vitest-environment jsdom
//
// The engine picker is PRIMARY — rendered before every field its engine
// choice shapes — because reading fields shaped by the engine before you've
// picked the engine doesn't make sense. This pins that order for the typed
// engine-having kinds in this lane: translate (engine-conditional
// source-language control, model picker, and its own destination) and ner
// (engine-conditional label controls).

import '@testing-library/jest-dom/vitest';
import { cleanup, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { columnDef } from '../support/domainFixtures';
import { sheetMeta } from '../support/actionFormFixtures';
import { mockLocalProviders, mountTypedForm } from './cutoverUiF1TypedForm';

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

function precedes(a: Element, b: Element): boolean {
  return Boolean(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
}

describe('engine picker renders PRIMARY for every engine-having kind', () => {
  it('translate: one execution picker precedes its destination and engine-conditional source-language control', async () => {
    dispose = mountTypedForm({
      kind: 'map.translate',
      sheet: sheetMeta([columnDef({ id: '1', name: 'statement', type: 'text' })], { id: '7' }),
    }).dispose;

    const enginePicker = screen.getByTestId('field-engine-model-choice');
    expect(enginePicker).toContainElement(screen.getByTestId('model-picker-button'));
    expect(screen.queryByTestId('engine-picker-button')).not.toBeInTheDocument();
    const destination = await screen.findByTestId('field-output-translation');
    const sourceLanguage = screen.getByTestId('translate-source-language-select');
    expect(precedes(enginePicker, destination)).toBe(true);
    expect(precedes(enginePicker, sourceLanguage)).toBe(true);
  });

  it('ner keeps its primary position ahead of the engine-conditional label controls', async () => {
    dispose = mountTypedForm({
      kind: 'map.ner',
      sheet: sheetMeta([columnDef({ id: '1', name: 'body', type: 'text' })], { id: '7' }),
    }).dispose;

    const enginePicker = screen.getByTestId('field-engine-model-choice');
    expect(enginePicker).toContainElement(screen.getByTestId('model-picker-button'));
    const nerFields = screen.getByTestId('ner-engine-fields');
    expect(precedes(enginePicker, nerFields)).toBe(true);
    await waitFor(() => expect(screen.getByTestId('field-output-entities')).toBeInTheDocument());
    expect(precedes(enginePicker, screen.getByTestId('field-output-entities'))).toBe(true);
  });
});
