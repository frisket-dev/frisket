// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ModelRouteControl, type ModelRouteSelection } from '../../src/engine-selector/ModelRouteControl';

afterEach(cleanup);

const selection: ModelRouteSelection = {
  model_id: 'google/gemini-flash',
  selected: 'cloud',
  can_mutate: true,
  options: [
    { id: 'cloud', label: 'Frisket Cloud', status: 'ready', disabled_reason: null },
    { id: 'own', label: 'Your Gemini key', status: 'needs_setup', disabled_reason: null },
  ],
};

describe('ModelRouteControl', () => {
  it('renders authoritative option order and refreshes after changing the model route', async () => {
    const setRoute = vi.fn().mockResolvedValue({ ...selection, selected: 'own' });
    let finishRefresh!: (result: boolean) => void;
    const onChanged = vi.fn(() => new Promise<boolean>((resolve) => { finishRefresh = resolve; }));
    const onBusyChange = vi.fn();
    render(<ModelRouteControl projectId="project-a" selection={selection}
      setRoute={setRoute} onChanged={onChanged} onBusyChange={onBusyChange} />);

    const buttons = screen.getAllByRole('button');
    expect(buttons.map((button) => button.textContent)).toEqual(['Frisket Cloud', 'Your Gemini key']);
    expect(screen.getByTestId('model-route-cloud')).toHaveAttribute('aria-pressed', 'true');

    await userEvent.click(screen.getByTestId('model-route-own'));
    await waitFor(() => expect(onChanged).toHaveBeenCalledOnce());
    expect(screen.getByTestId('model-route-own')).toBeDisabled();
    expect(onBusyChange.mock.calls.map(([busy]) => busy)).toEqual([true]);
    expect(setRoute).toHaveBeenCalledWith(
      'project-a', 'google/gemini-flash', 'own', expect.any(AbortSignal),
    );
    finishRefresh(true);
    await waitFor(() => expect(screen.getByTestId('model-route-own')).toBeEnabled());
    expect(onBusyChange.mock.calls.map(([busy]) => busy)).toEqual([true, false]);
  });

  it('keeps a server-disabled route visible without repeating its tooltip as body copy', () => {
    const blocked: ModelRouteSelection = {
      ...selection,
      selected: 'own',
      options: [
        { id: 'cloud', label: 'Frisket Cloud', status: 'unavailable',
          disabled_reason: 'OpenRouter isn’t offered through Frisket Cloud.' },
        { ...selection.options[1], label: 'Your OpenDocRouter API key' },
      ],
    };
    render(<ModelRouteControl projectId="project-a" selection={blocked}
      setRoute={vi.fn()} onChanged={vi.fn().mockResolvedValue(true)} />);

    expect(screen.getByTestId('model-route-cloud')).toBeDisabled();
    expect(screen.getByTestId('model-route-cloud')).toHaveAttribute(
      'title', 'OpenRouter isn’t offered through Frisket Cloud.',
    );
    expect(screen.queryByText('OpenRouter isn’t offered through Frisket Cloud.'))
      .not.toBeInTheDocument();
    expect(screen.getByTestId('model-route-own').querySelector('.segmented-option-label'))
      .toHaveTextContent('Your OpenDocRouter API key');
    expect(screen.getByTestId('model-route-own'))
      .toHaveAttribute('title', 'Your OpenDocRouter API key');
  });
});
