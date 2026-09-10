// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createProjectApi } from '../../src/api/real';
import { NowPlayingWidget } from '../../src/components/NowPlayingWidget';
import { FieldValue } from '../../src/components/RowDrawer';
import { createAudioPlaybackSource } from '../../src/state/audioPlaybackStore';
import { columnDef, row } from '../support/domainFixtures';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const projectId = 'audio-project';
const { render, stores } = createWorkspaceTestHarness({
  projectId,
  api: { projectApi: createProjectApi(projectId) },
});

const source = createAudioPlaybackSource({
  url: `/api/projects/${projectId}/blobs/${'a'.repeat(64)}`,
  label: 'hearing.mp3',
  sheetId: 'sheet-7',
  rowId: 'row-9',
  columnId: 'audio-3',
});

beforeEach(() => {
  stores.audioPlayback.dismiss();
  stores.route.projectExternal({
    projectId,
    sheetId: 'sheet-1',
    actionKind: null,
    review: false,
    panel: null,
  });
  vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue();
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {});
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('NowPlayingWidget', () => {
  it('owns the one media element, keeps native seek/progress controls, and dismisses cleanly', () => {
    const view = render(<NowPlayingWidget />);
    expect(screen.queryByTestId('now-playing-widget')).not.toBeInTheDocument();

    stores.audioPlayback.toggle(source);
    view.rerender(<NowPlayingWidget />);

    const audio = screen.getByTestId('now-playing-audio');
    expect(audio).toHaveAttribute('src', source.url);
    expect(audio).toHaveAttribute('controls');
    expect(audio).not.toHaveAttribute('autoplay');
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(1);
    expect(document.querySelectorAll('audio')).toHaveLength(1);

    fireEvent.pause(audio);
    expect(stores.audioPlayback.store.get().playing).toBe(false);
    fireEvent.play(audio);
    expect(stores.audioPlayback.store.get().playing).toBe(true);
    fireEvent.ended(audio);
    expect(stores.audioPlayback.store.get().playing).toBe(false);

    fireEvent.click(screen.getByRole('button', { name: 'Stop and dismiss audio' }));
    expect(HTMLMediaElement.prototype.pause).toHaveBeenCalled();
    expect(stores.audioPlayback.store.get().source).toBeNull();
    expect(screen.queryByTestId('now-playing-widget')).not.toBeInTheDocument();
  });

  it('reports playback failure and ignores an ended file as playing', () => {
    stores.audioPlayback.toggle(source);
    render(<NowPlayingWidget />);
    const audio = screen.getByTestId('now-playing-audio');

    fireEvent.error(audio);
    expect(screen.getByRole('alert')).toHaveTextContent('Audio playback failed.');
    expect(stores.audioPlayback.store.get().playing).toBe(false);
  });

  it('reports a rejected play attempt while that attempt is still current', async () => {
    vi.mocked(HTMLMediaElement.prototype.play).mockRejectedValueOnce(
      new DOMException('Playback is unavailable.', 'NotAllowedError'),
    );

    stores.audioPlayback.toggle(source);
    render(<NowPlayingWidget />);

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Audio playback could not start.',
    );
    expect(stores.audioPlayback.store.get()).toMatchObject({
      source,
      playing: false,
      error: 'Audio playback could not start.',
    });
  });

  it('ignores an interrupted play attempt after the same source is paused', async () => {
    let rejectPlay: ((reason?: unknown) => void) | undefined;
    vi.mocked(HTMLMediaElement.prototype.play).mockReturnValueOnce(new Promise(
      (_resolve, reject) => { rejectPlay = reject; },
    ));

    stores.audioPlayback.toggle(source);
    render(<NowPlayingWidget />);
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(1);

    act(() => stores.audioPlayback.toggle(source));
    expect(stores.audioPlayback.store.get()).toMatchObject({
      source,
      playing: false,
      error: null,
    });

    await act(async () => {
      rejectPlay?.(new DOMException('The play() request was interrupted.', 'AbortError'));
      await Promise.resolve();
    });

    expect(stores.audioPlayback.store.get()).toMatchObject({
      source,
      playing: false,
      error: null,
    });
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('opens the source row without stopping audio', () => {
    stores.audioPlayback.toggle(source);
    render(<NowPlayingWidget />);

    fireEvent.click(screen.getByRole('button', { name: /hearing\.mp3/ }));
    expect(stores.route.store.get()).toMatchObject({
      projectId,
      sheetId: source.sheetId,
      panel: { kind: 'row', rowId: source.rowId, columnId: source.columnId },
    });
    expect(stores.selection.store.get().selectedRows).toMatchObject({
      sheetId: source.sheetId,
      rowIds: [source.rowId],
    });
    expect(stores.audioPlayback.store.get()).toMatchObject({ source, playing: true });
  });

  it('makes row detail a control over the same app-level player', () => {
    const col = columnDef({ id: source.columnId, name: 'recording', type: 'audio' });
    const audioValue = JSON.stringify({
      blob: 'a'.repeat(64),
      filename: source.label,
      mime: 'audio/mpeg',
    });
    render(
      <>
        <FieldValue
          col={col}
          columns={[col]}
          row={row({ [col.id]: audioValue }, { id: source.rowId })}
          sheetId={source.sheetId}
          value={audioValue}
        />
        <NowPlayingWidget />
      </>,
    );

    fireEvent.click(screen.getByRole('button', { name: /Play recording audio/ }));
    expect(screen.getByRole('button', { name: /Pause recording audio/ })).toBeVisible();
    expect(screen.getByTestId('now-playing-audio')).toHaveAttribute('src', source.url);
    expect(document.querySelectorAll('audio')).toHaveLength(1);

    fireEvent.click(screen.getByRole('button', { name: /Pause recording audio/ }));
    expect(stores.audioPlayback.store.get()).toMatchObject({ source, playing: false });
    expect(document.querySelectorAll('audio')).toHaveLength(1);
  });
});
