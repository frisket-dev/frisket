// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it } from 'vitest';
import { useState } from 'react';

import { NerEngineFields } from '../../src/components/action-panel/NerEngineFields';

afterEach(cleanup);

function ControlledHarness() {
  const [engine, setEngine] = useState('gliner');
  const [labels, setLabels] = useState<string[]>(['person']);

  return (
    <>
      <button type="button" onClick={() => setEngine('gliner')}>GLiNER</button>
      <button type="button" onClick={() => setEngine('llm')}>LLM</button>
      <NerEngineFields
        engine={engine}
        labels={labels}
        onLabelsChange={setLabels}
      />
    </>
  );
}

describe('NerEngineFields', () => {
  it('keeps label authoring parent-controlled across engine-specific unmounts', async () => {
    const user = userEvent.setup();
    render(<ControlledHarness />);

    await user.type(screen.getByTestId('ner-gliner-labels-draft'), 'ship name{Enter}');
    expect(screen.getAllByTestId('ner-gliner-labels-chip')).toHaveLength(2);

    await user.click(screen.getByRole('button', { name: 'LLM' }));
    expect(screen.queryByTestId('ner-gliner-labels')).not.toBeInTheDocument();
    expect(screen.getAllByTestId('ner-llm-labels-chip')).toHaveLength(2);
    expect(screen.getByTestId('ner-llm-labels')).toHaveTextContent('person');
    expect(screen.getByTestId('ner-llm-labels')).toHaveTextContent('ship name');

    await user.click(screen.getByRole('button', { name: 'GLiNER' }));
    expect(screen.getAllByTestId('ner-gliner-labels-chip')).toHaveLength(2);
  });

  it('preserves the spaCy controls and their delegated blocking error', () => {
    render(
      <NerEngineFields
        engine="spacy"
        labels={['person']}
        labelsError="Choose at least one entity type."
        onLabelsChange={() => {}}
      />,
    );

    expect(screen.getByTestId('ner-engine-fields')).toBeVisible();
    expect(screen.getByTestId('ner-spacy-types-person')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByTestId('ner-spacy-types-error')).toHaveTextContent(
      'Choose at least one entity type.',
    );
  });

  it('keeps the optional model download attached only to the spaCy surface', () => {
    const spacyDownload = {
      artifact: {
        ref: 'spacy:en_core_web_sm',
        display_name: 'spaCy English pipeline',
        revision: 'abc123',
        size: 48_000_000,
        license: 'MIT',
      },
      onInstalled: () => {},
    };
    const { rerender } = render(
      <NerEngineFields
        engine="spacy"
        labels={['person']}
        onLabelsChange={() => {}}
        spacyDownload={spacyDownload}
      />,
    );

    expect(screen.getByTestId('engine-artifact-download')).toBeVisible();

    rerender(
      <NerEngineFields
        engine="gliner"
        labels={['person']}
        onLabelsChange={() => {}}
        spacyDownload={spacyDownload}
      />,
    );
    expect(screen.queryByTestId('engine-artifact-download')).not.toBeInTheDocument();
  });

  it('keeps GLiNER and LLM error test ids and accessible input names distinct', () => {
    const { rerender } = render(
      <NerEngineFields
        engine="gliner"
        labels={[]}
        labelsError="Choose a label."
        onLabelsChange={() => {}}
      />,
    );

    expect(screen.getByLabelText('Entity labels')).toBeVisible();
    expect(screen.getByTestId('ner-gliner-labels-error')).toHaveRole('alert');

    rerender(
      <NerEngineFields
        engine="llm"
        labels={[]}
        labelsError="Choose a label."
        onLabelsChange={() => {}}
      />,
    );
    expect(screen.getByLabelText('LLM entity labels')).toBeVisible();
    expect(screen.getByTestId('ner-llm-labels-error')).toHaveRole('alert');
  });
});
