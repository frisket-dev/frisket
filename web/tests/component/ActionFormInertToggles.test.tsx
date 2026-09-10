// @vitest-environment jsdom
//
// The confidence/justification checkboxes render only where the typed Params
// actually carry them: `include_justification` exists on ClassifyParams only;
// `include_confidence` on ClassifyParams and ExtractParams. Classify's local
// semantic engine reads neither, so its body hides both until the Model
// engine is chosen. Every other LLM kind renders neither — they were pure UI
// noise, never forwarded.
//
// Labels are the typed form's schema-derived ones ("Include Confidence"); the
// legacy form's hand-written "Include confidence" no longer exists.

import '@testing-library/jest-dom/vitest';
import { cleanup, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { columnDef } from '../support/domainFixtures';
import { sheetMeta } from '../support/actionFormFixtures';
import { installPopoverPolyfill } from '../support/domPolyfills';
import { mockActionApiDefaults } from '../support/renderActionForm';
import {
  chooseEngine,
  lastExecutedRequest,
  mockLocalProviders,
  mountTypedForm,
  pressRun,
} from './cutoverUiF1TypedForm';

let dispose: (() => void) | undefined;
afterEach(() => {
  cleanup();
  dispose?.();
  dispose = undefined;
  vi.restoreAllMocks();
});

beforeEach(() => {
  installPopoverPolyfill();
  mockActionApiDefaults();
  mockLocalProviders();
});

function sheet() {
  return sheetMeta([columnDef({ id: '1', name: 'notes', type: 'text' })], { id: '7' });
}

function mount(kind: string, initialDraft?: Parameters<typeof mountTypedForm>[0]['initialDraft']) {
  const mounted = mountTypedForm({ kind, sheet: sheet(), initialDraft });
  dispose = mounted.dispose;
  return mounted;
}

describe('confidence/justification toggle visibility (A20)', () => {
  it('classify shows both for the Model engine and neither for the local engine', async () => {
    const user = userEvent.setup();
    const { onExecute } = mount('map.classify');

    // Local semantic classification reads neither flag.
    expect(screen.getByTestId('model-picker-button')).toHaveTextContent('Local semantic');
    expect(screen.queryByLabelText('Include Confidence')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Include Justification')).not.toBeInTheDocument();

    await chooseEngine('llm');
    expect(screen.getByLabelText('Include Confidence')).toBeInTheDocument();
    expect(screen.getByLabelText('Include Justification')).toBeInTheDocument();

    // Both ride the typed Params, not a side channel.
    await user.click(screen.getByLabelText('Include Confidence'));
    await user.click(screen.getByLabelText('Include Justification'));
    await pressRun();
    const request = lastExecutedRequest(onExecute);
    expect(request.action_id).toBe('map.classify');
    expect(request.params).toMatchObject({
      engine: 'llm', include_confidence: true, include_justification: true,
    });
  });

  it('extract shows confidence only', async () => {
    const user = userEvent.setup();
    const { onExecute } = mount('map.extract');

    expect(screen.getByLabelText('Include Confidence')).toBeInTheDocument();
    expect(screen.queryByLabelText('Include Justification')).not.toBeInTheDocument();

    await user.click(screen.getByLabelText('Include Confidence'));
    await pressRun();
    const request = lastExecutedRequest(onExecute);
    expect(request.action_id).toBe('map.extract');
    expect(request.params).toMatchObject({ include_confidence: true });
    expect(request.params).not.toHaveProperty('include_justification');
  });

  it('tool-assisted extract shows confidence only', async () => {
    const user = userEvent.setup();
    // Mounted from a saved request: a FRESH map.mcp_extract mount crashes
    // (PRODUCT BUG pinned red in McpExtractActionWiring.test.tsx).
    const { onExecute } = mount('map.mcp_extract', {
      action_id: 'map.mcp_extract',
      scope: { kind: 'sheet_rows', sheet_id: 7 },
      params: { source: ['notes'], model: 'test/model', mcp_server_ids: ['local-crm'],
        fields: [{ name: 'value', type: 'text' }] },
      output_names: { value: 'value' },
    });

    expect(screen.getByLabelText('Include Confidence')).toBeInTheDocument();
    expect(screen.queryByLabelText('Include Justification')).not.toBeInTheDocument();

    await user.click(screen.getByLabelText('Include Confidence'));
    await pressRun();
    expect(lastExecutedRequest(onExecute).params).toMatchObject({ include_confidence: true });
  });

  it.each(['map.ner', 'map.translate', 'map.find', 'map.judge', 'research.answer', 'reduce.group_summary'])(
    '%s renders neither toggle',
    (kind) => {
      mount(kind);
      expect(screen.getByTestId('generated-action-form')).toBeVisible();
      if (kind === 'map.judge') expect(screen.getByTestId('field-guidelines')).toBeVisible();
      expect(screen.queryByLabelText('Include Confidence')).not.toBeInTheDocument();
      expect(screen.queryByLabelText('Include Justification')).not.toBeInTheDocument();
    },
  );
});
