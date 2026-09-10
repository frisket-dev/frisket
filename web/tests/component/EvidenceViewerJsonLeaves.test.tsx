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

describe('EvidenceViewer JSON leaves', () => {
  it('renders valid scalar/null JSON leaves without normalizing them into records', async () => {
    getEvidenceViewer.mockResolvedValue(jsonLeafPayload);

    render(<EvidenceViewer evidenceLinkId="evidence-link:1" mode="pane" onClose={() => {}} />);

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
      'not a quoted passage supporting the converted text',
    );
  });
});
