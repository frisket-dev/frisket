// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const getEvidenceViewer = vi.hoisted(() => vi.fn());

vi.mock('../../src/api/open', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api/open')>();
  return {
    ...actual,
    api: { ...actual.api, getEvidenceViewer },
  };
});

import { EvidenceViewer } from '../../src/components/EvidenceViewer';
import type { EvidenceViewerPayload } from '../../src/api/types';
import { createProjectApi } from '../../src/api/real';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const projectApi = createProjectApi('evidence-viewer-json-leaves');
vi.spyOn(projectApi, 'getEvidenceViewer').mockImplementation(getEvidenceViewer);
const { render } = createWorkspaceTestHarness({
  projectId: 'evidence-viewer-json-leaves',
  api: { projectApi: projectApi },
});

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const jsonLeafPayload: EvidenceViewerPayload = {
  schema_version: 'frisket.evidence_viewer.v1',
  link: {
    id: 1,
    stable_id: 'evidence-link:1',
    export_ref: 'evidence-link:1',
    subject_kind: 'cell',
    subject_ref: null,
    sheet_id: 1,
    row_id: 2,
    column_id: 3,
    run_id: null,
    op_id: null,
    receipt_id: null,
    role: 'support',
    status: 'active',
    confidence: null,
    pinned: false,
    producer: { action_kind: null, model: null, grounding_method: null },
    stale_reason: null,
    stale_at: null,
    created_at: '2026-08-14T00:00:00Z',
    text_layer_hash_mismatch: false,
  },
  artifacts: [
    {
      id: 1,
      stable_id: 'source_artifact:page',
      export_ref: 'source_artifact:page',
      artifact_kind: 'file',
      media_type: 'application/pdf',
      title: 'Page source',
      filename: null,
      page_count: 1,
      duration_ms: null,
      source_url: null,
      canonical_url: null,
      source_cell: null,
      external_ref: null,
      artifact_ref: {
        kind: 'source_artifact',
        stable_id: 'source_artifact:page',
        artifact_kind: 'file',
        media_type: 'application/pdf',
        blob: null,
        source_url: null,
        external_ref: null,
      },
      metadata: null,
      spans: [{
        id: 1,
        stable_id: 'evidence_span:page',
        export_ref: 'evidence_span:page',
        span_kind: 'region',
        rank: 0,
        span_role: 'support',
        required: true,
        note: null,
        status: 'active',
        selector: null,
        quote: null,
        snippet: null,
        text_layer_hash: null,
        preview: null,
        raw: 'unstructured raw detail',
        warnings: [],
        deep_link_url: null,
        clip_url: null,
        run_index: null,
      }],
      pages: [{
        page: 1,
        image: { blob_hash: 'page-image', url: '/page.png', width: null, height: null },
        text: null,
        regions: [{
          id: 1,
          stable_id: 'region:1',
          bbox: [
            null,
            4,
            { space: 'page_normalized', x0: 'not-a-number', y0: 0, x1: 1, y1: 1 },
            { space: 'page_normalized', x0: 0.1, y0: 0.2, x1: 0.6, y1: 0.8 },
          ],
          snippet: null,
          raw: null,
        }],
      }],
      runs: [],
    },
    {
      id: 2,
      stable_id: 'source_artifact:text',
      export_ref: 'source_artifact:text',
      artifact_kind: 'row',
      media_type: 'application/vnd.frisket.row+json',
      title: 'Text source',
      filename: null,
      page_count: null,
      duration_ms: null,
      source_url: null,
      canonical_url: null,
      source_cell: null,
      external_ref: false,
      artifact_ref: {
        kind: 'source_artifact',
        stable_id: 'source_artifact:text',
        artifact_kind: 'row',
        media_type: 'application/vnd.frisket.row+json',
        blob: null,
        source_url: null,
        external_ref: 0,
      },
      metadata: { source_columns: ['Description', null, 5, { ignored: true }, 'Summary'] },
      spans: [{
        id: 2,
        stable_id: 'evidence_span:text',
        export_ref: 'evidence_span:text',
        span_kind: 'text',
        rank: 0,
        span_role: 'support',
        required: true,
        note: null,
        status: 'active',
        selector: false,
        quote: 'A cited passage',
        snippet: 'A cited passage',
        text_layer_hash: null,
        preview: 0,
        raw: null,
        warnings: [],
        deep_link_url: null,
        clip_url: null,
        run_index: null,
      }],
      pages: [],
      runs: [],
    },
  ],
  warnings: [],
};

function staleTextPayload(
  artifactFields: Partial<EvidenceViewerPayload['artifacts'][number]> = {},
): EvidenceViewerPayload {
  const source: EvidenceViewerPayload['artifacts'][number] = {
    ...structuredClone(jsonLeafPayload.artifacts[1]),
    text_context_status: 'stale',
    recovery_status: null,
    recovery_notice: null,
    ...artifactFields,
  };
  return {
    ...jsonLeafPayload,
    link: {
      ...jsonLeafPayload.link,
      status: 'stale',
      stale_reason: 'source_changed',
    },
    artifacts: [source],
  };
}

describe('EvidenceViewer JSON leaves', () => {
  it('renders valid scalar/null JSON leaves without normalizing them into records', async () => {
    getEvidenceViewer.mockResolvedValue(jsonLeafPayload);

    render(<EvidenceViewer evidenceLinkId="evidence-link:1" mode="pane" onClose={() => {}} />);

    const detailsToggle = await screen.findByTestId('evidence-details-toggle');
    expect(screen.queryByTestId('evidence-viewer-detail-pane')).not.toBeInTheDocument();
    detailsToggle.click();
    expect(await screen.findByTestId('evidence-link-status')).toHaveTextContent('active');
    expect(screen.getAllByTestId('evidence-region-highlight')).toHaveLength(1);
    expect(screen.getAllByTestId('evidence-selector-summary')[0]).toHaveTextContent('null');
    expect(screen.getByText('raw', { selector: 'dt' })).toBeVisible();
    expect(screen.getByText('"unstructured raw detail"')).toBeVisible();
    expect(screen.getByTestId('evidence-text-field-label'))
      .toHaveTextContent('Candidate source columns: Description, Summary');
  });

  it('labels conversion lineage as provenance rather than supporting evidence', async () => {
    getEvidenceViewer.mockResolvedValue({
      ...jsonLeafPayload,
      link: { ...jsonLeafPayload.link, role: 'source_provenance' },
    });

    render(<EvidenceViewer evidenceLinkId="evidence-link:1" mode="pane" onClose={() => {}} />);

    expect(await screen.findByRole('heading', { name: 'Source provenance' })).toBeVisible();
    expect(screen.getByTestId('evidence-provenance-notice')).toHaveTextContent(
      'Original document used to create this text.',
    );
  });

  it('keeps raw OCR warning codes out of a saved text citation', async () => {
    const source = {
      ...jsonLeafPayload.artifacts[1],
      text_context: {
        text: 'A cited passage in the captured Markdown source.',
        offset_unit: 'utf16_code_unit',
        ranges: [{ span_id: 'evidence_span:text', start: 2, end: 16 }],
      },
    };
    getEvidenceViewer.mockResolvedValue({
      ...jsonLeafPayload,
      artifacts: [source],
      warnings: ['no_word_stream', 'internal_only_warning'],
    });

    render(<EvidenceViewer evidenceLinkId="evidence-link:1" mode="pane" onClose={() => {}} />);

    expect(await screen.findByTestId('evidence-text-body')).toHaveTextContent(source.text_context.text);
    expect(screen.queryByText('no_word_stream')).not.toBeInTheDocument();
    expect(screen.queryByText('internal_only_warning')).not.toBeInTheDocument();
    screen.getByTestId('evidence-details-toggle').click();
    expect(screen.queryByTestId('evidence-grounding-degraded')).not.toBeInTheDocument();
  });
});

describe('EvidenceViewer stale text recovery', () => {
  it('does not try to locate a stale passage while mounting the viewer', async () => {
    getEvidenceViewer.mockResolvedValue(staleTextPayload());

    render(<EvidenceViewer evidenceLinkId="evidence-link:1" mode="pane" onClose={() => {}} />);

    expect(await screen.findByRole('button', { name: 'Try to find this passage' })).not.toBeNull();
    expect(getEvidenceViewer).toHaveBeenCalledTimes(1);
    expect(getEvidenceViewer.mock.calls).toEqual([['evidence-link:1']]);
  });

  it('only requests current-source location after the button is clicked', async () => {
    let finishRecovery: ((payload: EvidenceViewerPayload) => void) | undefined;
    const recoveryResponse = new Promise<EvidenceViewerPayload>((resolve) => {
      finishRecovery = resolve;
    });
    getEvidenceViewer
      .mockResolvedValueOnce(staleTextPayload())
      .mockReturnValueOnce(recoveryResponse);

    render(<EvidenceViewer evidenceLinkId="evidence-link:1" mode="pane" onClose={() => {}} />);

    const button = await screen.findByRole('button', { name: 'Try to find this passage' });
    button.click();
    expect(getEvidenceViewer).toHaveBeenNthCalledWith(
      2,
      'evidence-link:1',
      { locateCurrent: true },
    );
    expect(
      (await screen.findByRole('button', { name: 'Looking for passage…' }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);

    finishRecovery?.(staleTextPayload({
      recovery_status: 'not_found',
      recovery_notice: 'Could not find this passage in the current source.',
      text_context: {
        text: 'The current source no longer contains the cited wording.',
        offset_unit: 'utf16_code_unit',
        ranges: [],
      },
    }));
    expect((await screen.findByTestId('evidence-text-recovery-not-found')).textContent)
      .toContain('Could not find this passage in the current source.');
  });

  it('shows recovered current context and highlights every returned match', async () => {
    getEvidenceViewer
      .mockResolvedValueOnce(staleTextPayload())
      .mockResolvedValueOnce(staleTextPayload({
        recovery_status: 'located',
        recovery_notice: 'Found two matches in the current source.',
        text_context: {
          text: 'Repeated passage, then the repeated passage again.',
          offset_unit: 'utf16_code_unit',
          ranges: [
            { span_id: 'evidence_span:text', start: 0, end: 16 },
            { span_id: 'evidence_span:text', start: 27, end: 43 },
          ],
        },
      }));

    render(<EvidenceViewer evidenceLinkId="evidence-link:1" mode="pane" onClose={() => {}} />);

    (await screen.findByRole('button', { name: 'Try to find this passage' })).click();
    expect((await screen.findByTestId('evidence-text-recovery-located')).textContent)
      .toContain('Found two matches in the current source.');
    expect(screen.getAllByTestId('evidence-text-highlight')).toHaveLength(2);
    expect(screen.getByText('support · stale')).not.toBeNull();
  });
});


describe('Ask evidence locations', () => {
  function citedPagePayload(): EvidenceViewerPayload {
    const source = structuredClone(jsonLeafPayload.artifacts[0]);
    source.spans[0].selector = { page_start: 2, page_end: 2 };
    source.pages = [
      { ...source.pages[0], page: 1, regions: [] },
      { ...source.pages[0], page: 2, regions: [{ ...source.pages[0].regions[0], stable_id: source.spans[0].stable_id }] },
    ];
    return { ...jsonLeafPayload, artifacts: [source] };
  }

  it('opens the cited page and highlights only its supporting region', async () => {
    getEvidenceViewer.mockResolvedValue(citedPagePayload());
    render(<EvidenceViewer evidenceLinkId="evidence-link:1" scopeSpanId="evidence_span:page" mode="pane" defaultShowDetails={false} onClose={() => {}} />);
    expect(await screen.findByText('Page 2', { exact: true })).toBeVisible();
    expect(screen.queryByText('Page 1', { exact: true })).not.toBeInTheDocument();
    expect(screen.getAllByTestId('evidence-region-highlight')).toHaveLength(1);
  });

  it('shows the source without old highlights when the cited value has changed', async () => {
    getEvidenceViewer.mockResolvedValue(citedPagePayload());
    render(<EvidenceViewer evidenceLinkId="evidence-link:1" scopeSpanId="evidence_span:page" highlight={false} mode="pane" defaultShowDetails={false} onClose={() => {}} />);
    expect(await screen.findByText('Page 2', { exact: true })).toBeVisible();
    expect(screen.getByText('Page 1', { exact: true })).toBeVisible();
    expect(screen.queryByTestId('evidence-region-highlight')).not.toBeInTheDocument();
  });
});
