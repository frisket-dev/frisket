// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { EmbeddingProvider, SheetMeta } from '../../src/api/types';
import { CreateEmbeddingIndexDialog } from '../../src/components/embeddings/CreateEmbeddingIndexDialog';
import type { SelectorChoice, HttpSelectorChoicesQuery } from '../../src/api/selectorChoices';

vi.mock('../../src/engine-selector/SelectorField', () => ({
  SelectorField: ({ query, onCurrentChoiceChange }: {
    query: HttpSelectorChoicesQuery;
    onCurrentChoiceChange(choice: SelectorChoice): void;
  }) => <button type="button" data-testid="check-embedding-choice"
    data-modality={query.subject.kind === 'embedding' ? query.subject.modality : ''}
    onClick={() => onCurrentChoiceChange({
      choice_id: 'text-model', label: 'FastEmbed', summary: '', description: '', facts: [],
      model_card_url: null, authored_selection: { kind: 'embedding', provider: 'fastembed', model: 'default' },
      resolved_target: null, processing_destination: { kind: 'local', label: 'On this device' },
      status: 'ready', can_author: true, can_run: true, blocker: null, setup: null,
      active_operation: null, is_current: true, is_default: false,
    })}>Check model</button>,
}));

afterEach(cleanup);

const providerCard: EmbeddingProvider = {
  providerId: 'fastembed', providerKind: 'local', modelId: 'default', label: 'FastEmbed',
  modalities: ['text'], dimensions: [384], local: true, available: true, disabledReason: null,
  egress: 'local', recommended: false, sizeGb: null, maxInputTokens: null,
  modalityCompatible: true, dimensionDiscoveryRequired: false,
};

const sheet = {
  id: '7', name: 'People', rowCount: 1,
  columns: [{ id: '11', name: 'bio', type: 'text' }],
  citedColumnIds: [], annotatedTextColumnIds: [],
} as SheetMeta;

describe('CreateEmbeddingIndexDialog custom model', () => {
  it('keeps sequential custom model input for a provider without an exact catalog card', () => {
    let model = 'custom';
    const rerenderDialog = (nextModel: string) => {
      model = nextModel;
      rerender(
        <CreateEmbeddingIndexDialog
          apiPort={{ getSheetData: vi.fn() }}
          sheet={sheet}
          form={{ provider: 'fastembed', model, sourceColumns: ['bio'], allowRemote: false,
            allowRemoteAutomaticRefresh: false, maxCost: '', confirmRemote: false }}
          selectedCard={null}
          providerCard={providerCard}
          remoteSelected={false}
          autoRefreshOn={false}
          createReady={false}
          creating={false}
          onClose={vi.fn()}
          onSubmit={vi.fn()}
          onModelSelect={(_provider, next) => rerenderDialog(next)}
          onSourceColumnToggle={vi.fn()}
          onAllowRemoteChange={vi.fn()}
          onAllowAutoRefreshChange={vi.fn()}
          onMaxCostChange={vi.fn()}
          onConfirmRemoteChange={vi.fn()}
        />,
      );
    };
    const { rerender } = render(<div />);
    rerenderDialog(model);

    const input = screen.getByTestId('embedding-model-custom-input');
    fireEvent.change(input, { target: { value: 'custom-a' } });
    expect(screen.getByTestId('embedding-model-custom-input')).toHaveValue('custom-a');
    fireEvent.change(screen.getByTestId('embedding-model-custom-input'), { target: { value: 'custom-ab' } });
    expect(screen.getByTestId('embedding-model-custom-input')).toHaveValue('custom-ab');
  });

  it('keeps text and Markdown source columns ready for an authoritative text model', () => {
    const mixedSheet = { ...sheet, columns: [
      ...sheet.columns, { id: '12', name: 'notes', type: 'markdown' },
    ] } as SheetMeta;
    render(<CreateEmbeddingIndexDialog
      apiPort={{ getSheetData: vi.fn() }}
      sheet={mixedSheet}
      form={{ provider: 'fastembed', model: 'default', sourceColumns: ['bio', 'notes'], allowRemote: false,
        allowRemoteAutomaticRefresh: false, maxCost: '', confirmRemote: false }}
      selectedCard={providerCard} providerCard={providerCard}
      remoteSelected={false} autoRefreshOn={false} createReady={true} creating={false}
      onClose={vi.fn()} onSubmit={vi.fn()} onModelSelect={vi.fn()} onSourceColumnToggle={vi.fn()}
      onAllowRemoteChange={vi.fn()} onAllowAutoRefreshChange={vi.fn()} onMaxCostChange={vi.fn()}
      onConfirmRemoteChange={vi.fn()}
    />);

    expect(screen.getByTestId('embedding-source-col-bio')).toBeChecked();
    expect(screen.getByTestId('embedding-source-col-notes')).toBeChecked();
    expect(screen.getByTestId('check-embedding-choice')).toHaveAttribute('data-modality', 'text');
    expect(screen.getByTestId('embedding-create-next')).toBeDisabled();
    fireEvent.click(screen.getByTestId('check-embedding-choice'));
    expect(screen.getByTestId('embedding-create-next')).toBeEnabled();
    fireEvent.click(screen.getByTestId('embedding-create-next'));
    expect(screen.getByTestId('embedding-create')).toBeEnabled();
    expect(screen.getByTestId('embedding-create-summary-columns')).toHaveTextContent('bio, notes');
  });
});
