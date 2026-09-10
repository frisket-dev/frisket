// @vitest-environment jsdom
//
// The sample project's Gallery captioned every tile "160" (2026-07-26
// hand-use pass). Nothing numeric was being read: the caption is the resolved
// media LABEL, which for a plain URL is its last path segment — and the sample
// seeds `https://picsum.photos/seed/frisket-a{i}/240/160`, whose last segment
// is the image height. The first thing we hand a new user, captioned with the
// same meaningless number 600 times.

import '@testing-library/jest-dom/vitest';
import { act, cleanup, screen, waitFor, render } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import type { Row } from '../../src/api/types';
import { mediaFromPluginViewCell } from '../../src/workbench/pluginViewContext';
import type { PluginViewContext } from '../../src/workbench/pluginViewContext';
import { ImageGallery } from '../../src/workbench/ImageGallery';



// A blob envelope resolves through blobUrl(), which needs a selected project.
function row(index: number, photo: string): Row {
  return {
    id: `row-${index}`,
    index,
    cells: { photo },
    provenance: {},
  } as unknown as Row;
}

function ctxFor(rows: Row[]): PluginViewContext {
  return {
    schemaVersion: 'frisket.plugin_view_context.v1',
    projectId: 'p1',
    contributionId: 'frisket.gallery',
    placement: { host: 'mainView', mode: 'pane' },
    sheet: {
      id: 's1',
      name: 'Articles',
      rowCount: rows.length,
      columns: [
        { id: 'title', name: 'title', type: 'text' },
        { id: 'photo', name: 'photo', type: 'image' },
      ],
    },
    selection: { selectedRowIds: [], activeRowId: null },
    rows: { query: async () => ({ rows, total: rows.length }) },
    media: { fromCell: mediaFromPluginViewCell },
    navigation: { openRow: () => {} },
  } as unknown as PluginViewContext;
}

afterEach(cleanup);

describe('image gallery captions', () => {
  it('shows an explicit loading state until the first image page arrives', async () => {
    let resolvePage!: (page: { rows: Row[]; total: number }) => void;
    const context = ctxFor([row(0, 'https://example.org/photo.jpg')]);
    context.rows.query = () => new Promise((resolve) => {
      resolvePage = resolve;
    });

    render(<ImageGallery ctx={context} />);

    expect(screen.getByTestId('gallery-view-loading')).toHaveTextContent('Loading images…');
    expect(screen.getByTestId('plugin-image-gallery-grid')).not.toBeVisible();

    await act(async () => {
      resolvePage({ rows: [row(0, 'https://example.org/photo.jpg')], total: 1 });
    });
    await waitFor(() => expect(screen.queryByTestId('gallery-view-loading')).toBeNull());
    expect(screen.getByTestId('plugin-image-gallery-tile')).toBeVisible();
  });

  it('captions nothing when the URL has no filename to show', async () => {
    render(
      <ImageGallery
        ctx={ctxFor([
          row(0, 'https://picsum.photos/seed/frisket-a0/240/160'),
          row(1, 'https://picsum.photos/seed/frisket-a1/240/160'),
        ])}
      />,
    );

    await waitFor(() =>
      expect(screen.getAllByTestId('plugin-image-gallery-tile')).toHaveLength(2),
    );
    expect(screen.queryByText('160')).toBeNull();
    expect(document.querySelectorAll('.plugin-image-gallery-label')).toHaveLength(0);
    // The tile keeps an accessible name even with no caption to show.
    expect(screen.getAllByTestId('plugin-image-gallery-tile')[0]).toHaveAttribute(
      'aria-label',
      'Row 1',
    );
  });

  it('captions a real filename, from a URL or from a blob envelope', async () => {
    render(
      <ImageGallery
        ctx={ctxFor([
          row(0, 'https://example.org/photos/mayor-at-podium.jpg'),
          row(1, JSON.stringify({ blob: 'sha256:abc', filename: 'scan-01.png', mime: 'image/png' })),
        ])}
      />,
    );

    await waitFor(() =>
      expect(screen.getAllByTestId('plugin-image-gallery-tile')).toHaveLength(2),
    );
    expect(screen.getByText('mayor-at-podium.jpg')).toBeVisible();
    expect(screen.getByText('scan-01.png')).toBeVisible();
  });
});
