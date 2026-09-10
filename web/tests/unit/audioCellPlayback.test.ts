import { GridCellKind } from '@glideapps/glide-data-grid';
import { describe, expect, it } from 'vitest';

import {
  audioMedia,
  buildCell,
  isAudioPlayButtonHit,
  withAudioPlayback,
  type MediaCellData,
} from '../../src/grid/cells';
import { columnDef, row } from '../support/domainFixtures';

describe('audio grid cell playback affordance', () => {
  it('uses the resolved cell media and decorates only the active playback view', () => {
    const col = columnDef({ id: 'audio', name: 'recording', type: 'audio' });
    const value = JSON.stringify({
      blob: 'b'.repeat(64),
      filename: 'meeting.mp3',
      mime: 'audio/mpeg',
    });
    const cell = buildCell(col, row({ audio: value }), { projectId: 'project-1' });

    expect(audioMedia(cell)).toEqual({
      url: `/api/projects/project-1/blobs/${'b'.repeat(64)}`,
      label: 'meeting.mp3',
    });
    const playing = withAudioPlayback(cell, true);
    expect(playing.kind).toBe(GridCellKind.Custom);
    expect((playing.data as MediaCellData).playback).toBe('playing');
    expect((cell.data as MediaCellData).playback).toBeUndefined();
  });

  it('reserves only the leading icon as the pointer play target', () => {
    expect(isAudioPlayButtonHit(20, 16, 32)).toBe(true);
    expect(isAudioPlayButtonHit(80, 16, 32)).toBe(false);
    expect(isAudioPlayButtonHit(20, 40, 32)).toBe(false);
  });
});
