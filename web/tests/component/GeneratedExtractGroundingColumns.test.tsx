// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import { generatedActionTemplateFromCatalogEntry } from '../../src/actions/model';
import { isGeneratedActionCatalogEntry } from '../../src/api/types';
import { GeneratedActionForm } from '../../src/components/action-panel/GeneratedActionForm';
import { ExtractParamsBody } from '../../src/components/action-panel/ExtractParamsBody';
import { sheetMeta } from '../support/actionFormFixtures';
import { syntheticActionCatalogEntry } from '../support/actionCatalogFixtures';
import { installPopoverPolyfill } from '../support/domPolyfills';
import { columnDef } from '../support/domainFixtures';

beforeAll(installPopoverPolyfill);
afterEach(cleanup);

describe('generated Extract grounding controls', () => {
  it('renders the typed source-document column selector in the grounding section', () => {
    const original = syntheticActionCatalogEntry('map.extract');
    const entry = {
      ...original,
      input_schema: {
        ...original.input_schema,
        properties: {
          ...original.input_schema.properties,
          source_document_columns: {
            type: 'array',
            items: { type: 'string' },
            default: [],
            title: 'Check citations against columns',
          },
        },
      },
      ui_hints: {
        ...original.ui_hints,
        semantic_controls: {
          ...original.ui_hints.semantic_controls,
          source_document_columns: 'columns',
        },
        source_requirements: [
          ...original.ui_hints.source_requirements,
          {
            id: 'source_document_columns',
            mode: 'columns' as const,
            param: 'source_document_columns',
            label: 'Check citations against columns',
            min: 0,
          },
        ],
      },
    };
    if (!isGeneratedActionCatalogEntry(entry)) {
      throw new Error('Extract test catalog is not generated');
    }
    const template = generatedActionTemplateFromCatalogEntry(entry);
    if (!template) throw new Error('Extract test template is missing');
    const sheet = sheetMeta([
      columnDef({ id: '1', name: 'body', type: 'text' }),
      columnDef({ id: '2', name: 'packet', type: 'file' }),
    ], { id: '7', rowCount: 1 });

    render(<GeneratedActionForm catalogEntry={entry} actionTemplate={template}
      sheet={sheet} selectedRowIds={['1']} initialSourceColumn="body" running={false}
      initialDraft={{
        action_id: 'map.extract',
        scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [1] },
        params: {
          source: ['body'], model: 'anthropic/test',
          fields: [{ name: 'fact', type: 'text' }],
          source_document_columns: ['packet'],
          grounding: {
            enabled: true, citation_required: true,
            allowed_methods: ['exact_quote'], include_stale_inputs: true,
          },
          evidence_policy: { citation_required: true },
        },
        output_names: { fact: 'Fact' },
      }}
      resolveParams={vi.fn(async () => ({
        diagnostics: {}, logical_outputs: entry.ui_hints.logical_outputs,
      }))}
      estimateAction={vi.fn(async () => ({ cost: 0, rows: 1, billed_cost: 0 }))}
      onExecute={vi.fn()} onClose={vi.fn()} />);

    expect(screen.getByText('Grounding and context')).toBeVisible();
    expect(screen.getByTestId('field-source_document_columns')).toBeInTheDocument();
    expect(screen.getByTestId('field-citation_mode')).toHaveValue('require');
  });

  it('derives the selector without rewriting saved grounding defaults', () => {
    const setParams = vi.fn();
    render(<ExtractParamsBody sheet={sheetMeta([], { id: '7', rowCount: 1 })}
      request={{ scope: { kind: 'sheet_rows', sheet_id: 7 }, output_names: {} }}
      params={{
        source: ['body'], model: 'anthropic/test', fields: [{ name: 'fact', type: 'text' }],
        grounding: { allowed_methods: ['exact_quote'], include_stale_inputs: true },
        evidence_policy: { citation_required: false },
      }}
      setParams={setParams} errors={{}}
      Field={({ name }) => <div data-testid={`field-${name}`} />} />);

    expect(screen.getByTestId('field-citation_mode')).toHaveValue('none');
    expect(setParams).not.toHaveBeenCalled();
  });

  it('requires citations when either saved policy requires them', () => {
    const setParams = vi.fn();
    render(<ExtractParamsBody sheet={sheetMeta([], { id: '7', rowCount: 1 })}
      request={{ scope: { kind: 'sheet_rows', sheet_id: 7 }, output_names: {} }}
      params={{
        source: ['body'], model: 'anthropic/test', fields: [{ name: 'fact', type: 'text' }],
        grounding: { enabled: true, citation_required: false },
        evidence_policy: { citation_required: true },
      }}
      setParams={setParams} errors={{}}
      Field={({ name }) => <div data-testid={`field-${name}`} />} />);

    expect(screen.getByTestId('field-citation_mode')).toHaveValue('require');
    expect(setParams).not.toHaveBeenCalled();
  });

  it.each([
    ['none', false, false],
    ['cite', true, false],
    ['require', true, true],
  ] as const)('switches to %s without dropping advanced grounding settings', (
    mode, enabled, citationRequired,
  ) => {
    const setParams = vi.fn();
    const sheet = sheetMeta([], { id: '7', rowCount: 1 });
    render(<ExtractParamsBody sheet={sheet}
      request={{ scope: { kind: 'sheet_rows', sheet_id: 7 }, output_names: {} }}
      params={{
        source: ['body'], model: 'anthropic/test', fields: [{ name: 'fact', type: 'text' }],
        grounding: {
          enabled: true, citation_required: true,
          allowed_methods: ['exact_quote'], include_stale_inputs: true,
        },
        evidence_policy: { citation_required: true },
        source_document_columns: ['packet'],
      }}
      setParams={setParams} errors={{}}
      Field={({ name }) => <div data-testid={`field-${name}`} />} />);

    fireEvent.change(screen.getByTestId('field-citation_mode'), {
      target: { value: mode },
    });

    expect(setParams).toHaveBeenCalledWith(expect.objectContaining({
      source_document_columns: ['packet'],
      grounding: {
        enabled,
        citation_required: citationRequired,
        allowed_methods: ['exact_quote'],
        include_stale_inputs: true,
      },
      evidence_policy: { citation_required: citationRequired },
    }));
  });
});
