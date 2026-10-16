// @vitest-environment jsdom

import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { AskCitation } from '../../src/api/projectQA';
import { useAskNavigation } from '../../src/workspace/useAskNavigation';

const mocks = vi.hoisted(() => ({
  navigate: vi.fn(),
  routeGet: vi.fn(),
  setGridView: vi.fn(),
  clearForGridTransition: vi.fn(),
  clearRowSelection: vi.fn(),
  setActivePromotedKey: vi.fn(),
  setAnswersViewSheetId: vi.fn(),
  setGridOnlySheetId: vi.fn(),
}));

vi.mock('../../src/bind/useWorkspaceStores', () => ({
  useWorkspaceStores: () => ({
    route: { navigate: mocks.navigate, store: { get: mocks.routeGet } },
    gridView: { store: { set: mocks.setGridView } },
    detail: { clearForGridTransition: mocks.clearForGridTransition },
    selection: { clearRowSelection: mocks.clearRowSelection },
    workView: {
      setActivePromotedKey: mocks.setActivePromotedKey,
      setAnswersViewSheetId: mocks.setAnswersViewSheetId,
      setGridOnlySheetId: mocks.setGridOnlySheetId,
    },
  }),
}));

const documentCitation = {
  id: 'citation-1',
  label: 'Meeting recording',
  source_kind: 'cell',
  excerpt: 'The vote begins.',
  status: 'current',
  message: null,
  target: {
    kind: 'cell',
    sheet_id: 1,
    row_id: 2,
    column_id: 3,
    navigation: {
      kind: 'document',
      sheet_id: 1,
      row_id: 2,
      column_id: 3,
      page: null,
      time_seconds: 17.25,
    },
    text_range: null,
  },
} satisfies AskCitation;

describe('Ask citation navigation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.routeGet.mockReturnValue({ sheetId: '4', panel: null });
  });

  it('opens a located cell citation at its exact document time', () => {
    const commands = {
      openEvidenceViewer: vi.fn(),
      setDocumentView: vi.fn(),
      setOpenSplit: vi.fn(),
    };
    const { result } = renderHook(() => useAskNavigation(
      [{ id: '1', name: 'Sources' } as never],
      commands,
    ));

    act(() => result.current.open(documentCitation));

    expect(mocks.navigate).toHaveBeenCalledWith(expect.objectContaining({
      sheetId: '1',
      panel: null,
    }));
    expect(commands.setDocumentView).toHaveBeenLastCalledWith(expect.objectContaining({
      sheetId: '1',
      sourceColumnId: '3',
      activeRowId: '2',
      readerTime: { rowId: '2', seconds: 17.25 },
    }));
  });

  it('keeps a stale cell citation on the ordinary row fallback', () => {
    const commands = {
      openEvidenceViewer: vi.fn(),
      setDocumentView: vi.fn(),
      setOpenSplit: vi.fn(),
    };
    const staleCitation: AskCitation = {
      ...documentCitation,
      status: 'changed',
      target: {
        kind: 'cell',
        sheet_id: 1,
        row_id: 2,
        column_id: 3,
        navigation: null,
        text_range: null,
      },
    };
    const { result } = renderHook(() => useAskNavigation(
      [{ id: '1', name: 'Sources' } as never],
      commands,
    ));

    act(() => result.current.open(staleCitation));

    expect(mocks.navigate).toHaveBeenCalledWith(expect.objectContaining({
      sheetId: '1',
      panel: { kind: 'row', rowId: '2', columnId: '3' },
    }));
    expect(commands.setDocumentView).toHaveBeenLastCalledWith(null);
  });
});
