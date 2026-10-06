// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

const pdf = vi.hoisted(() => ({ getDocument: vi.fn() }));

vi.mock('../../src/media/pdfjsSetup', () => ({
  pdfjsLib: { getDocument: pdf.getDocument, TextLayer: class {} },
}));

import { DocumentReader } from '../../src/workbench/DocumentReader';

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it('shows the original total while restricting navigation to the selected page', async () => {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
  vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(832);
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(900);
  const onPageCount = vi.fn();

  render(
    <DocumentReader
      media={{ url: '/blobs/report', label: 'report.pdf' }}
      mediaKind="pdf"
      title="report.pdf"
      layout="single"
      fit="width"
      videoFit="full"
      onVideoFitChange={() => undefined}
      textLayer={false}
      onPageCount={onPageCount}
      rowKey="page-two"
      onOpenDetail={() => undefined}
      canOpenDetail={false}
      optionsOpen={false}
      onToggleOptions={() => undefined}
      optionsPopover={null}
      selectionCount={0}
      initialPage={2}
      pageImages={[
        {
          page: 2,
          width: 800,
          height: 1000,
          url: '/blobs/report/pages/2/image',
        },
      ]}
      totalPageCount={10}
    />,
  );

  expect(await screen.findByRole('img', { name: 'Page 2' })).toBeInTheDocument();
  expect(screen.getByTestId('document-page-indicator')).toHaveTextContent('2 / 10');
  expect(screen.getByTestId('document-page-prev')).toBeDisabled();
  expect(screen.getByTestId('document-page-next')).toBeDisabled();
  expect(pdf.getDocument).not.toHaveBeenCalled();
  await waitFor(() => expect(onPageCount).toHaveBeenCalledWith('page-two', 10));
});
