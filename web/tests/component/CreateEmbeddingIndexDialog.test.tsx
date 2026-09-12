// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { EmbeddingProvider, SheetMeta } from '../../src/api/types';
import { CreateEmbeddingIndexDialog } from '../../src/components/embeddings/CreateEmbeddingIndexDialog';

vi.mock('../../src/engine-selector/SelectorField', () => ({ SelectorField: () => null }));

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
});
