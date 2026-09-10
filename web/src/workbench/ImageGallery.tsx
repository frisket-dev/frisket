import { useCallback, useEffect, useMemo, useState } from 'react';
import type { Row } from '../api/types';
import { mediaFilename } from '../media/resolveMediaValue';
import type { PluginViewContext } from './pluginViewContext';
import { PanelLoading } from '../components/PanelPrimitives';

const IMAGE_GALLERY_PAGE_SIZE = 12;

interface GalleryRow {
  row: Row;
  media: NonNullable<ReturnType<PluginViewContext['media']['fromCell']>>;
}

interface GalleryState {
  key: string;
  rows: Row[];
  total: number;
  loading: boolean;
  error: string | null;
}

const initialGalleryState = (
  key: string,
  rowCount: number,
  hasImageColumn: boolean,
): GalleryState => ({
  key,
  rows: [],
  total: rowCount,
  loading: hasImageColumn,
  error: null,
});

export function ImageGallery({ ctx }: { ctx: PluginViewContext }) {
  const imageColumn = useMemo(
    () => ctx.sheet.columns.find((column) => column.type === 'image') ?? null,
    [ctx.sheet.columns],
  );
  const galleryKey = `${ctx.sheet.id}:${imageColumn?.id ?? 'no-image-column'}`;
  const [state, setState] = useState<GalleryState>(() =>
    initialGalleryState(galleryKey, ctx.sheet.rowCount, imageColumn !== null),
  );
  const visibleState =
    state.key === galleryKey
      ? state
      : initialGalleryState(galleryKey, ctx.sheet.rowCount, imageColumn !== null);

  useEffect(() => {
    if (!imageColumn) return;
    let cancelled = false;
    void ctx.rows
      .query({
        columnIds: [imageColumn.id],
        offset: 0,
        limit: IMAGE_GALLERY_PAGE_SIZE,
      })
      .then((page) => {
        if (cancelled) return;
        setState({
          key: galleryKey,
          rows: page.rows,
          total: page.total,
          loading: false,
          error: null,
        });
      })
      .catch((err) => {
        if (cancelled) return;
        setState({
          key: galleryKey,
          rows: [],
          total: ctx.sheet.rowCount,
          loading: false,
          error: err instanceof Error ? err.message : 'Could not load images.',
        });
      });
    return () => {
      cancelled = true;
    };
  }, [ctx.rows, ctx.sheet.rowCount, galleryKey, imageColumn]);

  const loadNextPage = useCallback(async () => {
    if (!imageColumn || visibleState.loading || visibleState.rows.length >= visibleState.total) {
      return;
    }
    const offset = visibleState.rows.length;
    setState({
      ...visibleState,
      loading: true,
      error: null,
    });
    try {
      const page = await ctx.rows.query({
        columnIds: [imageColumn.id],
        offset,
        limit: IMAGE_GALLERY_PAGE_SIZE,
      });
      setState({
        key: galleryKey,
        rows: [...visibleState.rows, ...page.rows],
        total: page.total,
        loading: false,
        error: null,
      });
    } catch (err) {
      setState({
        ...visibleState,
        loading: false,
        error: err instanceof Error ? err.message : 'Could not load images.',
      });
    }
  }, [
    ctx.rows,
    galleryKey,
    imageColumn,
    visibleState,
  ]);

  if (!imageColumn) return null;

  const galleryRows: GalleryRow[] = [];
  for (const row of visibleState.rows) {
    const media = ctx.media.fromCell(row.cells[imageColumn.id] ?? null);
    if (media) galleryRows.push({ row, media });
  }
  const canLoadMore = visibleState.rows.length < visibleState.total;

  return (
    <div
      className="plugin-image-gallery"
      data-testid="plugin-image-gallery"
      data-context-schema-version={ctx.schemaVersion}
      data-contribution-id={ctx.contributionId}
      data-sheet-id={ctx.sheet.id}
      data-image-column-id={imageColumn.id}
      data-page-size={String(IMAGE_GALLERY_PAGE_SIZE)}
      data-loaded-row-count={String(visibleState.rows.length)}
      data-total-row-count={String(visibleState.total)}
    >
      <div className="plugin-image-gallery-header">
        <div>
          <h2>Image gallery</h2>
          <span className="muted">
            {imageColumn.name} · {galleryRows.length.toLocaleString()} visible
          </span>
        </div>
      </div>
      {visibleState.loading && visibleState.rows.length === 0 && (
        <PanelLoading
          className="main-view-loading"
          testId="gallery-view-loading"
          label="Loading images…"
        />
      )}
      {visibleState.error && (
        <div className="plugin-image-gallery-message" role="alert">
          {visibleState.error}
        </div>
      )}
      <div
        className="plugin-image-gallery-grid"
        data-testid="plugin-image-gallery-grid"
        hidden={visibleState.loading && visibleState.rows.length === 0}
      >
        {galleryRows.map(({ row, media }) => {
          // A filename, or nothing. `media.label` falls back to a URL's last
          // path segment, which for the sample project's
          // `…/seed/frisket-a1/240/160` is the image HEIGHT — every tile
          // captioned "160". The tile keeps an accessible name either way.
          const caption = mediaFilename(media);
          return (
            <button
              key={row.id}
              type="button"
              className="plugin-image-gallery-tile"
              data-testid="plugin-image-gallery-tile"
              data-row-id={row.id}
              data-blob-hash={media.blobHash}
              aria-label={caption ?? `Row ${row.index + 1}`}
              onClick={() => ctx.navigation.openRow(row.id)}
            >
              <span className="plugin-image-gallery-thumb">
                <img src={media.url} alt="" loading="lazy" />
              </span>
              {caption ? (
                <span className="plugin-image-gallery-label">{caption}</span>
              ) : null}
            </button>
          );
        })}
      </div>
      <div
        className="plugin-image-gallery-footer"
        hidden={visibleState.loading && visibleState.rows.length === 0}
      >
        <button
          type="button"
          className="mini-btn"
          data-testid="plugin-image-gallery-load-more"
          disabled={!canLoadMore || visibleState.loading}
          onClick={() => void loadNextPage()}
        >
          {visibleState.loading ? 'Loading' : canLoadMore ? 'Load more' : 'All images loaded'}
        </button>
      </div>
    </div>
  );
}
