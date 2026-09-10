import { afterEach, describe, expect, it, vi } from 'vitest';

const raw = vi.hoisted(() => ({
  googleOAuthStartUrl: vi.fn(),
  mapErrorFactory: undefined as undefined | ((status: number, message: string) => Error),
  getMapPoints: vi.fn(),
  getMapPointsArrowBuffer: vi.fn(),
  projectExportUrl: vi.fn(),
  sheetDatasetExportUrl: vi.fn(),
  workLogExportUrl: vi.fn(),
  embeddingExportArtifactUrl: vi.fn(),
}));
const browser = vi.hoisted(() => ({
  signOut: vi.fn(async () => undefined),
}));

vi.mock('../../src/api/raw/browserAuth', () => ({
  googleOAuthStartUrl: raw.googleOAuthStartUrl,
}));
vi.mock('../../src/api/browserAuth', () => ({
  signOut: browser.signOut,
}));
vi.mock('../../src/api/raw/mapPointsArrow', () => ({
  createMapPointsArrowApi: vi.fn((errorFactory) => {
    raw.mapErrorFactory = errorFactory;
    return {
      getMapPoints: raw.getMapPoints,
      getMapPointsArrowBuffer: raw.getMapPointsArrowBuffer,
    };
  }),
}));
vi.mock('../../src/api/raw/projectResources', () => ({
  projectExportUrl: raw.projectExportUrl,
  sheetDatasetExportUrl: raw.sheetDatasetExportUrl,
  workLogExportUrl: raw.workLogExportUrl,
  embeddingExportArtifactUrl: raw.embeddingExportArtifactUrl,
}));

import { ApiError, signOut, createProjectApi } from '../../src/api/real';

const realApi = createProjectApi('team / one');

afterEach(() => {
  vi.resetAllMocks();
});

describe('real API raw-boundary composition', () => {
  it('delegates browser logout to its typed contract adapter', async () => {
    await signOut();

    expect(browser.signOut).toHaveBeenCalledOnce();
  });

  it('delegates Google OAuth navigation to its named raw owner', () => {
    raw.googleOAuthStartUrl.mockReturnValue('google-oauth-start-url');

    expect(realApi.googleOAuthStartUrl()).toBe('google-oauth-start-url');
    expect(raw.googleOAuthStartUrl).toHaveBeenCalledOnce();
  });

  it('delegates map Arrow reads and browser resource URLs with the captured project id', async () => {
    raw.getMapPoints.mockResolvedValue({ count: 0, positions: new Float32Array(), rowIds: [], attributes: {} });
    raw.getMapPointsArrowBuffer.mockResolvedValue(new ArrayBuffer(0));
    raw.embeddingExportArtifactUrl.mockReturnValue('embedding-url');
    raw.projectExportUrl.mockReturnValue('project-export-url');
    raw.sheetDatasetExportUrl.mockReturnValue('sheet-dataset-url');
    raw.workLogExportUrl.mockReturnValue('work-log-url');

    await expect(realApi.getMapPoints('sheet-1', 'column-1')).resolves.toMatchObject({ count: 0 });
    await expect(realApi.getMapPointsArrowBuffer('sheet-1', 'column-1')).resolves.toBeInstanceOf(ArrayBuffer);
    expect(realApi.embeddingExportArtifactUrl('index-1', 'parquet')).toBe('embedding-url');
    expect(realApi.projectExportUrl(false)).toBe('project-export-url');
    expect(realApi.sheetDatasetExportUrl({ format: 'xlsx', sheetIds: ['sheet-1'] })).toBe(
      'sheet-dataset-url',
    );
    expect(realApi.workLogExportUrl('pdf')).toBe('work-log-url');

    expect(raw.getMapPoints).toHaveBeenCalledWith('sheet-1', 'column-1', {});
    expect(raw.getMapPointsArrowBuffer).toHaveBeenCalledWith('sheet-1', 'column-1', {});
    const mapError = raw.mapErrorFactory?.(422, 'map column is unavailable');
    expect(mapError).toBeInstanceOf(ApiError);
    expect(mapError).toMatchObject({
      status: 422,
      message: 'map column is unavailable',
      code: undefined,
      details: undefined,
    });
    expect(raw.embeddingExportArtifactUrl).toHaveBeenCalledWith('team / one', 'index-1', 'parquet');
    expect(raw.projectExportUrl).toHaveBeenCalledWith('team / one', false);
    expect(raw.sheetDatasetExportUrl).toHaveBeenCalledWith('team / one', {
      format: 'xlsx',
      sheetIds: ['sheet-1'],
    });
    expect(raw.workLogExportUrl).toHaveBeenCalledWith('team / one', 'pdf');
  });
});
