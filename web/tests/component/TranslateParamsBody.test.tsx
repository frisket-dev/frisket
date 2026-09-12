// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { useState } from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeAll, expect, it, vi } from 'vitest';

import type { EngineOption } from '../../src/api/types';
import type { GeneratedActionParams } from '../../src/generated/actionTypes';
import { TranslateParamsBody } from '../../src/components/action-panel/TranslateParamsBody';
import { sheetMeta } from '../support/actionFormFixtures';
import { installPopoverPolyfill } from '../support/domPolyfills';

beforeAll(installPopoverPolyfill);
afterEach(cleanup);
type Params = GeneratedActionParams['map.translate'];
const engine: EngineOption = {
  id: 'opus_mt', label: 'Opus-MT', tier: 'local', available: true,
  models: ['en-es', 'es-en'],
  downloadable_pairs: [
    { pair: 'en-es', display_name: 'English → Spanish', size: 100, license: 'CC-BY-4.0' },
    { pair: 'es-en', display_name: 'Spanish → English', size: 100, license: 'CC-BY-4.0' },
  ],
};
function Harness({ initial, onChange }: { initial: Params; onChange(params: Params): void }) {
  const [params, setParams] = useState(initial);
  const update = (next: Params) => { setParams(next); onChange(next); };
  return <TranslateParamsBody sheet={sheetMeta([])} request={{
    scope: { kind: 'sheet_rows', sheet_id: 1 },
  }} params={params} setParams={update} engine={engine} errors={{}}
  Field={({ name }) => name === 'engine'
    ? <button type="button" data-testid="field-engine" onClick={() => update({ ...params, engine: 'hy_mt2' })}>Choose Hy-MT2</button>
    : <div data-testid={`field-${name}`} />} />;
}

it('keeps LLM fields and optional detection visible by default', () => {
  const onChange = vi.fn();
  render(<Harness initial={{ source: ['text'], model: 'test/model' }} onChange={onChange} />);
  expect(screen.getByTestId('field-engine')).toBeInTheDocument();
  expect(screen.getByTestId('field-context')).toBeInTheDocument();
  expect(screen.getByTestId('field-save_detected_language')).toBeInTheDocument();
  expect(onChange).not.toHaveBeenCalled();
});

it('clears only unsupported intent on an explicit Hy-MT2 switch', async () => {
  const onChange = vi.fn();
  render(<Harness initial={{ source: ['text'], engine: 'llm', model: 'test/model',
    context: 'newsroom', target_language: 'French', language: ['en'], save_detected_language: true }}
  onChange={onChange} />);
  // Engine selection is delegated to the host Field; the body owns cleanup.
  fireEvent.click(screen.getByRole('button', { name: 'Choose Hy-MT2' }));
  await waitFor(() => expect(onChange.mock.lastCall?.[0]).toEqual({ source: ['text'], engine: 'hy_mt2',
    target_language: 'French', save_detected_language: false }));
  expect(screen.queryByTestId('field-model')).not.toBeInTheDocument();
  expect(screen.queryByTestId('field-context')).not.toBeInTheDocument();
  expect(screen.queryByTestId('field-save_detected_language')).not.toBeInTheDocument();
});

it('swaps both languages atomically without dropping either edit', () => {
  const onChange = vi.fn();
  render(<Harness initial={{ source: ['text'], engine: 'opus_mt', language: ['en'],
    target_language: 'es' }} onChange={onChange} />);
  fireEvent.click(screen.getByTestId('translate-pair-swap'));
  expect(onChange.mock.lastCall?.[0]).toMatchObject({ language: ['es'], target_language: 'en' });
  expect(screen.getByTestId('translate-pair-source')).toHaveValue('es');
  expect(screen.getByTestId('translate-pair-target')).toHaveValue('en');
});
