// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { GeneratedActionCatalogEntry, RunEstimate } from '../../src/api/types';
import { generatedActionTemplateFromCatalogEntry } from '../../src/actions/model';
import { GeneratedActionForm } from '../../src/components/action-panel/GeneratedActionForm';
import { syntheticActionCatalogEntry } from '../support/actionCatalogFixtures';
import { sheetMeta } from '../support/actionFormFixtures';
import { aiMeta, columnDef } from '../support/domainFixtures';

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function regexCatalogEntry(): GeneratedActionCatalogEntry {
  return syntheticActionCatalogEntry('map.regex_extract', {
    title: 'Extract regex',
    cost_policy: { kind: 'model_metered', requires_confirmation: true, notes: null },
    input_schema: {
      type: 'object',
      required: ['input_columns', 'pattern'],
      properties: {
        input_columns: { type: 'array', title: 'Input columns', items: { type: 'string' } },
        pattern: { type: 'string', title: 'Pattern', default: '\\d+' },
      },
    },
    ui_hints: {
      form: 'generated',
      category: 'text',
      semantic_controls: { input_columns: 'columns' },
      source_requirements: [{
        id: 'input_columns',
        mode: 'columns',
        param: 'input_columns',
        label: 'Input columns',
        min: 1,
        accepted_column_types: ['text'],
      }],
      logical_outputs: [{ key: 'extracted', column_type: 'text' }],
    },
  }) as GeneratedActionCatalogEntry;
}

describe('GeneratedActionForm view scope and view backfill', () => {
  it('defaults to view_id, displays honest estimated row count, runs on view_id, and triggers view-scoped backfill', async () => {
    const entry = regexCatalogEntry();
    const template = generatedActionTemplateFromCatalogEntry(entry);
    if (!template) throw new Error('Expected generated action template');
    const sheet = sheetMeta([
      columnDef({ id: '11', name: 'story', type: 'text' }),
      columnDef({
        id: '12',
        name: 'extracted',
        type: 'text',
        ai: aiMeta({
          versions: [
            {
              version: 1,
              runId: '10',
              model: 'local',
              startedAt: '2026-11-22T00:00:00Z',
              rowCount: 2,
              cost: 0,
              status: 'complete',
            },
          ],
        }),
      }),
    ], { id: '7', name: 'Filings', rowCount: 10 });

    const estimateAction = vi.fn(async (): Promise<RunEstimate> => ({
      cost: 0,
      cost_source: 'free_local',
      rows: 3,
      requires_confirmation: false,
    }));
    const onExecute = vi.fn();
    const onBackfill = vi.fn();

    render(
      <GeneratedActionForm
        catalogEntry={entry}
        actionTemplate={template}
        sheet={sheet}
        initialDraft={{
          action_id: 'map.regex_extract',
          scope: { kind: 'sheet_rows', sheet_id: 7 },
          params: { input_columns: ['story'], pattern: '\\d+' },
          output_names: { extracted: 'extracted' },
        }}
        viewId={42}
        viewName="High Priority"
        hasExactRowScopeInitializer
        running={false}
        resolveParams={async () => ({ diagnostics: {}, logical_outputs: [{ key: 'extracted', column_type: 'text' }] })}
        estimateAction={estimateAction}
        onExecute={onExecute}
        onBackfill={onBackfill}
        onClose={() => {}}
      />,
    );

    await waitFor(() => {
      expect(estimateAction).toHaveBeenCalledWith(
        expect.objectContaining({
          action_id: 'map.regex_extract',
          scope: { kind: 'sheet_rows', sheet_id: 7, view_id: 42 },
        }),
      );
    });

    await waitFor(() => {
      expect(screen.getByText(/3 rows in view “High Priority”/i)).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId('generated-action-run'));
    await waitFor(() => {
      expect(onExecute).toHaveBeenCalledWith(
        expect.objectContaining({
          action_id: 'map.regex_extract',
          scope: { kind: 'sheet_rows', sheet_id: 7, view_id: 42 },
        }),
        'run',
      );
    });

    // Scope menu exposes both Run on saved View and Backfill new rows in view.
    fireEvent.click(screen.getByTestId('generated-action-run-scope-menu-button'));
    expect(screen.getByTestId('generated-action-row-scope-selected')).toHaveTextContent('Run on “High Priority”');
    const backfillBtn = screen.getByTestId('generated-action-row-scope-backfill');
    expect(backfillBtn).not.toBeDisabled();
    expect(backfillBtn).toHaveTextContent('Backfill new rows in view');

    fireEvent.click(backfillBtn);
    expect(onBackfill).toHaveBeenCalledWith('extracted', undefined, 42);
  });

  it('allows running on view without hasExactRowScopeInitializer and hides full-sheet count on view option when switched to all rows', async () => {
    const entry = regexCatalogEntry();
    const template = generatedActionTemplateFromCatalogEntry(entry);
    if (!template) throw new Error('Expected generated action template');
    const sheet = sheetMeta([
      columnDef({ id: '11', name: 'story', type: 'text' }),
    ], { id: '7', name: 'Filings', rowCount: 10 });

    const estimateAction = vi.fn(async (req: { scope: { kind: string; view_id?: number } }): Promise<RunEstimate> => ({
      cost: 0,
      cost_source: 'free_local',
      rows: req.scope.kind === 'sheet_rows' && req.scope.view_id === 42 ? 3 : 10,
      requires_confirmation: false,
    }));
    const onExecute = vi.fn();

    render(
      <GeneratedActionForm
        catalogEntry={entry}
        actionTemplate={template}
        sheet={sheet}
        initialDraft={{
          action_id: 'map.regex_extract',
          scope: { kind: 'sheet_rows', sheet_id: 7 },
          params: { input_columns: ['story'], pattern: '\\d+' },
          output_names: { extracted: 'extracted' },
        }}
        viewId={42}
        viewName="High Priority"
        running={false}
        resolveParams={async () => ({ diagnostics: {}, logical_outputs: [{ key: 'extracted', column_type: 'text' }] })}
        estimateAction={estimateAction}
        onExecute={onExecute}
        onClose={() => {}}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText(/3 rows in view “High Priority”/i)).toBeInTheDocument();
    });

    // Submitting without hasExactRowScopeInitializer still executes view-scoped request.
    fireEvent.click(screen.getByTestId('generated-action-run'));
    await waitFor(() => {
      expect(onExecute).toHaveBeenCalledWith(
        expect.objectContaining({
          scope: { kind: 'sheet_rows', sheet_id: 7, view_id: 42 },
        }),
        'run',
      );
    });

    // Switch scope to all rows (10 rows); the View menu item must NOT show "10 rows".
    fireEvent.click(screen.getByTestId('generated-action-run-scope-menu-button'));
    fireEvent.click(screen.getByTestId('generated-action-row-scope-all'));

    await waitFor(() => {
      expect(estimateAction).toHaveBeenCalledWith(
        expect.objectContaining({
          scope: { kind: 'sheet_rows', sheet_id: 7 },
        }),
      );
    });

    fireEvent.click(screen.getByTestId('generated-action-run-scope-menu-button'));
    const viewOption = screen.getByTestId('generated-action-row-scope-selected');
    expect(viewOption).toHaveTextContent('Current view');
    expect(viewOption).not.toHaveTextContent('10 rows');
  });
});
