// @vitest-environment jsdom
//
// A text/plain file in a Document view used to render as a download-link icon:
// `documentMediaKind` had no text branch, so every text file fell through to
// `other`. It was the one document kind the Document READER could not show you
// (observed in a hand-use pass — shots/37b-document-view.png).
//
// Pins the classification and that the reader renders the file's own bytes,
// plus the two things that are easy to get wrong: a declared non-text mime is
// NOT overridden by a `.txt` suffix, and a failed fetch still leaves the
// download link reachable.

import '@testing-library/jest-dom/vitest';
import { cleanup, screen, within, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

// pdf.js touches DOMMatrix at import time, which jsdom does not provide; the
// text branch never reaches it. Stubbed at the module seam so this test can
// mount the REAL DocumentReader and pin the routing, rather than only the
// extracted viewer (the bug was the routing).
vi.mock('../../src/media/pdfjsSetup', () => ({ pdfjsLib: { getDocument: () => {}, TextLayer: class {} } }));

import { DocumentReader } from '../../src/workbench/DocumentReader';
import { documentMediaKind } from '../../src/workbench/documentMedia';
import type { ResolvedMediaValue } from '../../src/media/resolveMediaValue';



afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const TEXT_MEDIA: ResolvedMediaValue = {
  url: '/api/projects/p1/blobs/sha256-notes',
  label: 'notes.txt',
  filename: 'notes.txt',
  mime: 'text/plain',
};

function stubFetch(body: string | Error, ok = true) {
  const fetchMock = vi.fn(() =>
    body instanceof Error
      ? Promise.reject(body)
      : Promise.resolve({ ok, status: ok ? 200 : 404, text: () => Promise.resolve(body) }),
  );
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function renderReader(media: ResolvedMediaValue) {
  return render(
    <DocumentReader
      media={media}
      mediaKind={documentMediaKind(media, 'file')}
      title={media.filename ?? media.label}
      layout="continuous"
      fit="width"
      videoFit="full"
      onVideoFitChange={() => {}}
      textLayer={false}
      onPageCount={() => {}}
      rowKey="row-1"
      onOpenDetail={() => {}}
      canOpenDetail={false}
      optionsOpen={false}
      onToggleOptions={() => {}}
      optionsPopover={null}
      selectionCount={0}
    />,
  );
}

describe('documentMediaKind text classification', () => {
  it('classifies a text/* mime as text', () => {
    expect(documentMediaKind(TEXT_MEDIA, 'file')).toBe('text');
    expect(
      documentMediaKind({ url: '/b', label: 'a.csv', mime: 'text/csv' }, 'file'),
    ).toBe('text');
  });

  it('falls back to the suffix ONLY when no mime was declared', () => {
    expect(documentMediaKind({ url: '/b', label: 'a.md', filename: 'a.md' }, 'file')).toBe('text');
    // A declared binary mime is a claim; a .txt suffix does not get to override
    // it, or a mislabelled blob renders as mojibake instead of a download link.
    expect(
      documentMediaKind(
        { url: '/b', label: 'a.txt', filename: 'a.txt', mime: 'application/octet-stream' },
        'file',
      ),
    ).toBe('other');
  });

  it('still prefers pdf/image/video/audio over text', () => {
    expect(documentMediaKind({ url: '/b', label: 'a.pdf', mime: 'application/pdf' }, 'file')).toBe(
      'pdf',
    );
    expect(documentMediaKind({ url: '/b', label: 'a.png', mime: 'image/png' }, 'file')).toBe(
      'image',
    );
  });
});

describe('DocumentReader text file body', () => {
  it('renders the file contents, not a download link', async () => {
    stubFetch('Ada Lovelace met Charles Babbage.\nSecond line.');
    renderReader(TEXT_MEDIA);

    const body = await screen.findByTestId('document-text-body');
    expect(body).toHaveTextContent('Ada Lovelace met Charles Babbage.');
    expect(screen.queryByTestId('document-file')).not.toBeInTheDocument();
    expect(screen.getByTestId('document-reader-chip')).toHaveTextContent('Text');
  });

  it('keeps the download link reachable when the fetch fails', async () => {
    stubFetch('nope', false);
    renderReader(TEXT_MEDIA);

    const error = await screen.findByTestId('document-text-file-error');
    expect(error).toHaveTextContent('Could not load this file (404).');
    expect(within(error).getByRole('link', { name: 'notes.txt' })).toHaveAttribute(
      'href',
      TEXT_MEDIA.url,
    );
  });
});
