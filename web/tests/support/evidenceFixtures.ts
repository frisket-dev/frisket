// Typed evidence fixtures for component tests. Built against the real
// EvidenceArtifact / EvidenceSpan / EvidenceArtifactRef wire types so a
// contract change breaks the fixture rather than letting a stale shape drift.

import type {
  EvidenceArtifact,
  EvidenceArtifactRef,
  EvidenceBlobRef,
  EvidenceSpan,
} from '../../src/api/types';

let seq = 0;

export function evidenceSpan(overrides: Partial<EvidenceSpan> = {}): EvidenceSpan {
  seq += 1;
  return {
    id: seq,
    stable_id: overrides.stable_id ?? `evidence_span:${seq}`,
    export_ref: overrides.export_ref ?? `evidence_span:${seq}`,
    span_kind: 'text',
    rank: 0,
    span_role: 'support',
    required: true,
    note: null,
    status: 'active',
    selector: {},
    quote: null,
    snippet: null,
    text_layer_hash: null,
    preview: {},
    raw: {},
    warnings: [],
    deep_link_url: null,
    clip_url: null,
    run_index: null,
    ...overrides,
  };
}

export function artifactRef(overrides: Partial<EvidenceArtifactRef> = {}): EvidenceArtifactRef {
  return {
    kind: 'source_artifact',
    stable_id: overrides.stable_id ?? 'source_artifact:1',
    artifact_kind: overrides.artifact_kind ?? 'row',
    media_type: overrides.media_type ?? 'application/vnd.frisket.row+json',
    blob: overrides.blob ?? null,
    source_url: null,
    external_ref: {},
    ...overrides,
  };
}

export function blobRef(url: string, overrides: Partial<EvidenceBlobRef> = {}): EvidenceBlobRef {
  return { hash: 'blob-hash', url, filename: 'notes.txt', ...overrides };
}

export function evidenceArtifact(overrides: Partial<EvidenceArtifact> = {}): EvidenceArtifact {
  seq += 1;
  return {
    id: seq,
    stable_id: overrides.stable_id ?? `source_artifact:${seq}`,
    export_ref: overrides.export_ref ?? `source_artifact:${seq}`,
    artifact_kind: overrides.artifact_kind ?? 'row',
    media_type: overrides.media_type ?? 'application/vnd.frisket.row+json',
    title: null,
    filename: null,
    page_count: null,
    duration_ms: null,
    source_url: null,
    canonical_url: null,
    source_cell: null,
    external_ref: {},
    artifact_ref: overrides.artifact_ref ?? artifactRef(),
    metadata: {},
    spans: overrides.spans ?? [],
    pages: [],
    runs: [],
    ...overrides,
  };
}
