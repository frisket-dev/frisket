// @vitest-environment jsdom
//
// The `Extract frames` media op writes a json column whose value is a list of
// frame records ({t,image:{blob,mime,filename}}). The row drawer used to show
// that as a table of blob-hash text (JsonMiniTable). These tests pin the new
// behaviour: a homogeneous list of image envelopes renders as <img> thumbnails
// pointed at the project blob URL, while a list of non-image objects still
// falls through to the text table.

import '@testing-library/jest-dom/vitest';
import { cleanup, screen, within } from '@testing-library/react';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import { PreviewFieldValue, RowField } from '../../src/components/RowDrawer';
import { createProjectApi } from '../../src/api/real';
import { columnDef, row } from '../support/domainFixtures';
import { installPopoverPolyfill } from '../support/domPolyfills';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const projectApi = createProjectApi('proj-frames');
const { render } = createWorkspaceTestHarness({
  projectId: 'proj-frames',
  api: { projectApi: projectApi },
});

beforeAll(() => {
  installPopoverPolyfill();
  // blobUrl() needs a selected project to build /api/projects/{pid}/blobs/...
});

// RowField mounts CellEvidenceBlock, which fetches on mount — isolate from the
// network exactly as RowField.test.tsx does.
beforeEach(() => {
  vi.stubGlobal('fetch', () => Promise.reject(new Error('no network in unit test')));
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const noopEdit = () => Promise.resolve();

describe('row drawer json image-blob thumbnails', () => {
  it('renders a list of image-blob envelopes as thumbnails pointed at the blob URL', () => {
    const col = columnDef({ id: 'frames', name: 'frames', type: 'json' });
    const frames = [
      { image: { blob: 'a'.repeat(64), mime: 'image/jpeg', filename: 'frame0.jpg' }, t: 79.65 },
      { image: { blob: 'b'.repeat(64), mime: 'image/jpeg', filename: 'frame1.jpg' }, t: 158.2 },
    ];
    render(
      <RowField
        col={col}
        columns={[col]}
        row={row({ frames: JSON.stringify(frames) })}
        selected
        onEdit={noopEdit}
      />,
    );
    const field = screen.getByTestId('row-field-frames');

    const thumbs = within(field).getAllByTestId('json-blob-thumb');
    expect(thumbs).toHaveLength(2);
    // Each thumbnail is a real <img> pointing at the content-addressed blob route.
    for (const thumb of thumbs) {
      expect(thumb.tagName).toBe('IMG');
      expect(thumb).toHaveAttribute('loading', 'lazy');
    }
    expect(thumbs[0]).toHaveAttribute(
      'src',
      `/api/projects/proj-frames/blobs/${'a'.repeat(64)}`,
    );
    expect(thumbs[1]).toHaveAttribute(
      'src',
      `/api/projects/proj-frames/blobs/${'b'.repeat(64)}`,
    );

    // The filename is surfaced as a human caption, not the raw hash.
    expect(within(field).getByText('frame0.jpg')).toBeInTheDocument();
    expect(thumbs[0]).toHaveAttribute('alt', expect.stringContaining('1:19'));
    // No blob-hash text table is rendered for the image list.
    expect(within(field).queryByTestId('json-mini-table')).toBeNull();
    expect(field.textContent).not.toContain('a'.repeat(64));
  });

  it('renders bounded preview bytes without inventing blob URLs and visibly retains omitted frame times', () => {
    const inline = 'data:image/jpeg;base64,/9j/2Q==';
    render(<PreviewFieldValue preview={{
      column: { name: 'frames', columnType: 'json', format: null, hidden: false, overwritesColumnId: null },
      cell: { value: JSON.stringify([
      { t: 1, image: { inline_data_url: inline, mime: 'image/jpeg', filename: 'preview.jpg' } },
      { t: 2, image: { preview_omitted: true, mime: 'image/jpeg', filename: 'omitted.jpg' } },
      ]) },
    }} />);
    expect(screen.getByTestId('preview-field-value')).toHaveTextContent('frames · Preview');
    expect(screen.getByTestId('json-blob-thumb')).toHaveAttribute('src', inline);
    expect(screen.getByTestId('json-blob-thumb-omitted')).toHaveTextContent('limit');
    expect(screen.getByText('omitted.jpg').parentElement).toHaveTextContent('0:02');
    expect(screen.queryByTestId('json-mini-table')).not.toBeInTheDocument();
  });

  it.each([
    [{ x: 1, y: 2, w: 3, h: 4, face: { blob: 'a'.repeat(64), mime: 'image/jpeg', filename: 'face.jpg' } }],
    [{ t: 1, image: { blob: 'data:image/jpeg;base64,/9j/2Q==', mime: 'image/jpeg', filename: 'bad.jpg' } }],
    [{ t: 1, image: { inline_data_url: 'https://example.test/image.jpg', mime: 'image/jpeg', filename: 'bad.jpg' } }],
  ])('keeps geometry and malformed image payloads in ordinary JSON: %j', (...items) => {
    const col = columnDef({ id: 'results', name: 'results', type: 'json' });
    render(<RowField col={col} columns={[col]} row={row({ results: JSON.stringify(items) })}
      selected onEdit={noopEdit} />);
    expect(screen.queryByTestId('json-blob-thumb')).not.toBeInTheDocument();
    expect(screen.getByTestId('json-mini-table')).toBeInTheDocument();
  });

  it('still renders a list of non-image objects as the text table (no thumbnails)', () => {
    const col = columnDef({ id: 'results', name: 'results', type: 'json' });
    const results = [
      { title: 'City budget approved', url: 'https://example.com/a' },
      { title: 'Zoning appeal filed', url: 'https://example.com/b' },
    ];
    render(
      <RowField
        col={col}
        columns={[col]}
        row={row({ results: JSON.stringify(results) })}
        selected
        onEdit={noopEdit}
      />,
    );
    const field = screen.getByTestId('row-field-results');

    expect(within(field).queryByTestId('json-blob-thumb')).toBeNull();
    expect(within(field).getByTestId('json-mini-table')).toBeInTheDocument();
    expect(within(field).getByText('City budget approved')).toBeInTheDocument();
  });
});
