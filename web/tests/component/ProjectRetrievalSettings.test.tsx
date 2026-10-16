// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ProjectRetrievalSettings as ProjectRetrievalSettingsValue } from '../../src/api/types';
import { ProjectRetrievalSettings } from '../../src/settings/ProjectRetrievalSettings';

const SETTINGS: ProjectRetrievalSettingsValue = {
  text_enabled: true,
  transcript_enabled: true,
  image_enabled: true,
  pdf_pages_enabled: true,
  video: {
    strategy: 'shot_clips',
    interval_seconds: null,
    duration_seconds: 10,
    stride_seconds: null,
    sample_fps: 1,
  },
  audio_enabled: false,
};

afterEach(cleanup);

describe('ProjectRetrievalSettings', () => {
  it('renders the saved modalities and every supported video strategy', () => {
    render(<ProjectRetrievalSettings settings={SETTINGS} canMutate onPatch={vi.fn()} />);

    expect(screen.getByRole('checkbox', { name: 'Text cells' })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: 'Existing transcripts' })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: 'Images' })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: 'PDF pages' })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: /Raw audio/ })).not.toBeChecked();
    expect(screen.getByText(/Optional fallback/)).toBeInTheDocument();

    const video = screen.getByTestId('project-retrieval-video-strategy');
    expect(video).toHaveValue('shot_clips');
    expect(Array.from((video as HTMLSelectElement).options).map(({ value }) => value)).toEqual([
      'off',
      'frames',
      'fixed_clips',
      'shot_clips',
    ]);
  });

  it('sends narrow typed patches for source toggles and video strategy changes', async () => {
    const onPatch = vi.fn();
    const user = userEvent.setup();
    render(<ProjectRetrievalSettings settings={SETTINGS} canMutate onPatch={onPatch} />);

    await user.click(screen.getByRole('checkbox', { name: 'Text cells' }));
    await user.click(screen.getByRole('checkbox', { name: /Raw audio/ }));
    await user.selectOptions(screen.getByTestId('project-retrieval-video-strategy'), 'fixed_clips');

    expect(onPatch.mock.calls).toEqual([
      [{ retrieval: { text_enabled: false } }],
      [{ retrieval: { audio_enabled: true } }],
      [{ retrieval: { video: { strategy: 'fixed_clips' } } }],
    ]);
  });

  it('keeps every control read-only without owner permission', () => {
    render(<ProjectRetrievalSettings settings={SETTINGS} canMutate={false} onPatch={vi.fn()} />);

    for (const control of screen.getAllByRole('checkbox')) expect(control).toBeDisabled();
    expect(screen.getByTestId('project-retrieval-video-strategy')).toBeDisabled();
  });
});
