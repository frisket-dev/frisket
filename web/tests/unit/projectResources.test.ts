import { describe, expect, it } from 'vitest';

import {
  embeddingExportArtifactUrl,
  projectBlobUrl,
  projectExportUrl,
  sheetDatasetExportUrl,
  workLogExportUrl,
} from '../../src/api/raw/projectResources';

describe('project resource URLs', () => {
  const projectId = 'project / one?';

  it('builds the named blob and embedding resource URLs', () => {
    expect(projectBlobUrl(projectId, 'sha/256?')).toBe(
      '/api/projects/project%20%2F%20one%3F/blobs/sha%2F256%3F',
    );
    expect(embeddingExportArtifactUrl(projectId, 'index / one?', 'parquet/json')).toBe(
      '/api/projects/project%20%2F%20one%3F/embeddings/v1/indexes/index%20%2F%20one%3F/export/parquet%2Fjson',
    );
  });

  it('preserves bundle-or-db and work-log resource query and extension behavior', () => {
    expect(projectExportUrl(projectId)).toBe(
      '/api/projects/project%20%2F%20one%3F/export?mode=bundle&include_media=true&include_traces=false',
    );
    expect(projectExportUrl(projectId, false)).toBe(
      '/api/projects/project%20%2F%20one%3F/export?mode=bundle&include_media=false&include_traces=false',
    );
    expect(projectExportUrl(projectId, { includeTraces: true })).toBe(
      '/api/projects/project%20%2F%20one%3F/export?mode=bundle&include_media=true&include_traces=true',
    );
    expect(projectExportUrl(projectId, { mode: 'db' })).toBe(
      '/api/projects/project%20%2F%20one%3F/export?mode=db',
    );
    expect(workLogExportUrl(projectId, 'html')).toBe(
      '/api/projects/project%20%2F%20one%3F/export/work-log.html',
    );
    expect(workLogExportUrl(projectId, 'pdf')).toBe(
      '/api/projects/project%20%2F%20one%3F/export/work-log.pdf',
    );
    expect(workLogExportUrl(projectId)).toBe(
      '/api/projects/project%20%2F%20one%3F/export/work-log.md',
    );
  });

  it('builds the canonical selected-sheet export URL for CSV or Excel', () => {
    expect(
      sheetDatasetExportUrl(projectId, {
        format: 'csv',
        sheetIds: ['2', '7', '2'],
      }),
    ).toBe(
      '/api/projects/project%20%2F%20one%3F/exports/sheets?sheet_id=2&sheet_id=7&format=csv',
    );
    expect(
      sheetDatasetExportUrl(projectId, {
        format: 'xlsx',
        sheetIds: ['sheet / one?'],
        currentView: {
          filter: { status: { eq: 'ready now' } },
          sort: [{ column: 'created at', dir: 'desc' }],
        },
      }),
    ).toBe(
      '/api/projects/project%20%2F%20one%3F/exports/sheets?sheet_id=sheet+%2F+one%3F&format=xlsx&filter=%7B%22status%22%3A%7B%22eq%22%3A%22ready+now%22%7D%7D&sort=%5B%7B%22column%22%3A%22created+at%22%2C%22dir%22%3A%22desc%22%7D%5D',
    );
    expect(
      sheetDatasetExportUrl(projectId, { format: 'csv', sheetIds: ['one'] }),
    ).toBe('/api/projects/project%20%2F%20one%3F/exports/sheets?sheet_id=one&format=csv');
    expect(() =>
      sheetDatasetExportUrl(projectId, { format: 'xlsx', sheetIds: [] }),
    ).toThrow('select at least one sheet');
  });
});
