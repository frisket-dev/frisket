// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

vi.mock('../../src/media/pdfjsSetup', () => ({
  pdfjsLib: { getDocument: () => {}, TextLayer: class {} },
}));

import { DocumentReader } from '../../src/workbench/DocumentReader';

afterEach(() => cleanup());

it.each([
  ['video', 'document-video'],
  ['audio', 'document-audio'],
] as const)('seeks an unlinked %s result after metadata loads', (mediaKind, testId) => {
  const { rerender } = render(
    <DocumentReader
      media={{ url: `/blobs/result.${mediaKind}`, label: `result.${mediaKind}` }}
      mediaKind={mediaKind}
      title={`result.${mediaKind}`}
      layout="continuous"
      fit="width"
      videoFit="full"
      onVideoFitChange={() => undefined}
      textLayer={false}
      onPageCount={() => undefined}
      rowKey="row-1"
      onOpenDetail={() => undefined}
      canOpenDetail={false}
      optionsOpen={false}
      onToggleOptions={() => undefined}
      optionsPopover={null}
      selectionCount={0}
      initialTime={12.5}
    />,
  );

  const media = screen.getByTestId(testId).querySelector('video, audio');
  expect(media).not.toBeNull();
  fireEvent.loadedMetadata(media as HTMLMediaElement);
  expect((media as HTMLMediaElement).currentTime).toBe(12.5);

  rerender(
    <DocumentReader
      media={{ url: `/blobs/result.${mediaKind}`, label: `result.${mediaKind}` }}
      mediaKind={mediaKind}
      title={`result.${mediaKind}`}
      layout="continuous"
      fit="width"
      videoFit="full"
      onVideoFitChange={() => undefined}
      textLayer={false}
      onPageCount={() => undefined}
      rowKey="row-1"
      onOpenDetail={() => undefined}
      canOpenDetail={false}
      optionsOpen={false}
      onToggleOptions={() => undefined}
      optionsPopover={null}
      selectionCount={0}
      initialTime={21}
    />,
  );
  expect((media as HTMLMediaElement).currentTime).toBe(21);
});

it.each([
  ['video', 'document-video'],
  ['audio', 'document-audio'],
] as const)('anchors Find similar to the current %s time', (mediaKind, testId) => {
  const onFindSimilar = vi.fn();
  render(
    <DocumentReader
      media={{ url: `/blobs/result.${mediaKind}`, label: `result.${mediaKind}` }}
      mediaKind={mediaKind}
      title={`result.${mediaKind}`}
      layout="continuous"
      fit="width"
      videoFit="full"
      onVideoFitChange={() => undefined}
      textLayer={false}
      onPageCount={() => undefined}
      rowKey="row-1"
      onOpenDetail={() => undefined}
      canOpenDetail={false}
      optionsOpen={false}
      onToggleOptions={() => undefined}
      optionsPopover={null}
      selectionCount={0}
      onFindSimilar={onFindSimilar}
    />,
  );

  const media = screen.getByTestId(testId).querySelector('video, audio') as HTMLMediaElement;
  media.currentTime = 20.25;
  fireEvent.timeUpdate(media);
  fireEvent.click(screen.getByTestId('document-find-similar'));
  expect(onFindSimilar).toHaveBeenCalledWith({ kind: mediaKind, timeSeconds: 20.25 });
});
