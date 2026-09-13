// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import type { EmbeddingProvider, SheetMeta } from '../../src/api/types';
import { CreateEmbeddingIndexDialog } from '../../src/components/embeddings/CreateEmbeddingIndexDialog';
import type { HttpSelectorChoicesQuery, HttpSelectorChoicesResponse } from '../../src/api/selectorChoices';
import { installDialogPolyfill } from '../support/domPolyfills';

const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock('../../src/api/httpContract', () => ({ httpContract: request }));
beforeAll(installDialogPolyfill);
afterEach(() => { cleanup(); vi.resetAllMocks(); localStorage.clear(); });

function serveChoices(otherProviders = false) {
  request.mockImplementation((id: string, options: { body: HttpSelectorChoicesQuery }) => {
    if (id !== 'tenant.selector_choices.post' || options.body.subject.kind !== 'embedding') {
      throw new Error(`Unexpected HTTP operation: ${id}`);
    }
    const subject = options.body.subject;
    const provider = subject.provider ?? 'fastembed';
    const model = subject.model ?? 'default';
    const choiceId = `${provider}:${model}`;
    const response: HttpSelectorChoicesResponse = {
      schema_version: 'frisket.selector_choices.v1', project_id: 'embedding-project',
      subject: { kind: 'embedding', provider, model }, depends_on: [],
      current_choice_id: choiceId, default_choice_id: choiceId, orphaned_current: null,
      groups: [{ group_id: provider, kind: 'local', label: provider, status: 'ready', choices: [{
        choice_id: choiceId, label: 'Embedding model', summary: '', description: '', facts: [],
        model_card_url: null, authored_selection: { kind: 'embedding', provider, model },
        resolved_target: null, processing_destination: { kind: 'local', label: 'On this device' },
        status: 'ready', can_author: true, can_run: true, blocker: null, setup: null,
        active_operation: null, is_current: true, is_default: false,
      }] }],
    };
    if (otherProviders) {
      const current = response.groups[0].choices[0];
      response.groups.push(...['openai', 'cohere'].map((other) => ({
        group_id: other, kind: 'provider' as const, label: other, status: 'ready' as const,
        choices: [{ ...current, choice_id: `${other}:default`, label: `${other} embedding`,
          authored_selection: { kind: 'embedding' as const, provider: other, model: 'default' },
          is_current: false }],
      })));
    }
    return Promise.resolve(response);
  });
}

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
  it('keeps provider-bound opaque custom model editing inside selector details', async () => {
    serveChoices();
    const onModelSelect = vi.fn();
    let model = 'custom';
    const rerenderDialog = (nextModel: string) => {
      model = nextModel;
      rerender(
        <CreateEmbeddingIndexDialog
          projectId="embedding-project"
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
          onModelSelect={(provider, next) => { onModelSelect(provider, next); rerenderDialog(next); }}
          onSourceColumnToggle={vi.fn()}
          onAllowRemoteChange={vi.fn()}
          onAllowAutoRefreshChange={vi.fn()}
          onMaxCostChange={vi.fn()}
          onConfirmRemoteChange={vi.fn()}
        />,
      );
    };
    const { rerender } = render(<div />);
    await act(async () => { rerenderDialog(model); });
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /Embedding model/ })); });
    const dialog = screen.getByTestId('engine-selector-dialog');
    const input = within(dialog).getByRole('textbox', { name: 'Custom model ID' });
    expect(input).toHaveValue('custom');
    await act(async () => { fireEvent.change(input, { target: { value: 'publisher/nested/embedding-a' } }); });
    expect(onModelSelect).not.toHaveBeenCalled();
    expect(within(dialog).getByRole('textbox', { name: 'Custom model ID' })).toHaveValue('publisher/nested/embedding-a');
    await act(async () => { fireEvent.change(within(dialog).getByRole('textbox', { name: 'Custom model ID' }), {
      target: { value: 'publisher/nested/embedding-ab' },
    }); });
    expect(onModelSelect).not.toHaveBeenCalled();
    expect(within(dialog).getByRole('textbox', { name: 'Custom model ID' })).toHaveValue('publisher/nested/embedding-ab');
    await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: 'Use model' })); });
    expect(onModelSelect).toHaveBeenLastCalledWith('fastembed', 'publisher/nested/embedding-ab');
    expect(screen.queryByTestId('engine-selector-dialog')).not.toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: 'Custom model ID' })).not.toBeInTheDocument();
  });


  async function openProviderDetails() {
    serveChoices(true);
    const select = vi.fn();
    await act(async () => { render(<CreateEmbeddingIndexDialog projectId="embedding-project"
      apiPort={{ getSheetData: vi.fn() }} sheet={sheet}
      form={{ provider: 'fastembed', model: 'custom', sourceColumns: ['bio'], allowRemote: false,
        allowRemoteAutomaticRefresh: false, maxCost: '', confirmRemote: false }}
      selectedCard={null} providerCard={providerCard} remoteSelected={false} autoRefreshOn={false}
      createReady={false} creating={false} onClose={vi.fn()} onSubmit={vi.fn()}
      onModelSelect={select} onSourceColumnToggle={vi.fn()} onAllowRemoteChange={vi.fn()}
      onAllowAutoRefreshChange={vi.fn()} onMaxCostChange={vi.fn()} onConfirmRemoteChange={vi.fn()} />); });
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /Embedding model/ })); });
    const dialog = screen.getByTestId('engine-selector-dialog');
    await act(async () => { fireEvent.change(within(dialog).getByRole('searchbox'), { target: { value: 'embedding' } }); });
    await act(async () => { fireEvent.focus(within(dialog).getByRole('button', { name: /openai embedding/ })); });
    return { dialog, select };
  }

  it('clears an uncommitted custom ID when explicit navigation changes its provider', async () => {
    const { dialog, select } = await openProviderDetails();
    const input = within(dialog).getByRole('textbox', { name: 'Custom model ID' });
    expect(input).toHaveValue('');
    fireEvent.change(input, { target: { value: 'publisher/private/embedding' } });
    expect(input).toHaveValue('publisher/private/embedding');
    await act(async () => { fireEvent.focus(within(dialog).getByRole('button', { name: /cohere embedding/ })); });
    expect(within(dialog).getByRole('heading', { name: 'cohere embedding' })).toBeVisible();
    expect(within(dialog).getByRole('textbox', { name: 'Custom model ID' })).toHaveValue('');
    expect(select).not.toHaveBeenCalled();
  });

  it('keeps an edited custom-ID detail pinned through blur and incidental hover', async () => {
    const { dialog, select } = await openProviderDetails();
    const input = within(dialog).getByRole('textbox', { name: 'Custom model ID' });
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: 'publisher/private/embedding' } });
    fireEvent.blur(input);
    fireEvent.mouseEnter(within(dialog).getByRole('button', { name: /cohere embedding/ }));
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 150)); });
    expect(within(dialog).getByRole('heading', { name: 'openai embedding' })).toBeVisible();
    expect(within(dialog).getByRole('textbox', { name: 'Custom model ID' })).toHaveValue('publisher/private/embedding');
    expect(select).not.toHaveBeenCalled();
  });

  it('keeps text and Markdown source columns ready for an authoritative text model', async () => {
    serveChoices();
    const mixedSheet = { ...sheet, columns: [
      ...sheet.columns, { id: '12', name: 'notes', type: 'markdown' },
    ] } as SheetMeta;
    await act(async () => { render(<CreateEmbeddingIndexDialog
      projectId="embedding-project"
      apiPort={{ getSheetData: vi.fn() }}
      sheet={mixedSheet}
      form={{ provider: 'fastembed', model: 'default', sourceColumns: ['bio', 'notes'], allowRemote: false,
        allowRemoteAutomaticRefresh: false, maxCost: '', confirmRemote: false }}
      selectedCard={providerCard} providerCard={providerCard}
      remoteSelected={false} autoRefreshOn={false} createReady={true} creating={false}
      onClose={vi.fn()} onSubmit={vi.fn()} onModelSelect={vi.fn()} onSourceColumnToggle={vi.fn()}
      onAllowRemoteChange={vi.fn()} onAllowAutoRefreshChange={vi.fn()} onMaxCostChange={vi.fn()}
      onConfirmRemoteChange={vi.fn()}
    />); });

    expect(screen.getByTestId('embedding-source-col-bio')).toBeChecked();
    expect(screen.getByTestId('embedding-source-col-notes')).toBeChecked();
    expect(request).toHaveBeenCalledWith('tenant.selector_choices.post', expect.objectContaining({
      body: expect.objectContaining({ subject: expect.objectContaining({ modality: 'text' }) }),
    }));
    expect(screen.getByTestId('embedding-create-next')).toBeEnabled();
    fireEvent.click(screen.getByTestId('embedding-create-next'));
    expect(screen.getByTestId('embedding-create')).toBeEnabled();
    expect(screen.getByTestId('embedding-create-summary-columns')).toHaveTextContent('bio, notes');
  });
});
