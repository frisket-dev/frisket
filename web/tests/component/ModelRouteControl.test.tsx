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
    const onChanged = vi.fn();
    const onEditingChange = vi.fn();
    render(<ModelRouteControl projectId="project-a" selection={selection}
      setRoute={setRoute} onChanged={onChanged} onEditingChange={onEditingChange} />);

    const buttons = screen.getAllByRole('button');
    expect(buttons.map((button) => button.textContent)).toEqual(['Frisket Cloud', 'Your Gemini key']);
    expect(screen.getByTestId('model-route-cloud')).toHaveAttribute('aria-pressed', 'true');

    await userEvent.click(screen.getByTestId('model-route-own'));
    await waitFor(() => expect(onChanged).toHaveBeenCalledOnce());
    expect(setRoute).toHaveBeenCalledWith(
      'project-a', 'google/gemini-flash', 'own', expect.any(AbortSignal),
    );
    expect(onEditingChange.mock.calls.map(([editing]) => editing)).toEqual([true, false]);
  });

  it('keeps a server-disabled route visible and explains why it cannot be selected', () => {
    const blocked: ModelRouteSelection = {
      ...selection,
      selected: 'own',
      options: [
        { id: 'cloud', label: 'Frisket Cloud', status: 'unavailable',
          disabled_reason: 'OpenRouter isn’t offered through Frisket Cloud.' },
        selection.options[1],
      ],
    };
    render(<ModelRouteControl projectId="project-a" selection={blocked}
      setRoute={vi.fn()} onChanged={vi.fn()} />);

    expect(screen.getByTestId('model-route-cloud')).toBeDisabled();
    expect(screen.getByTestId('model-route-cloud')).toHaveAttribute(
      'title', 'OpenRouter isn’t offered through Frisket Cloud.',
    );
    expect(screen.getByText('OpenRouter isn’t offered through Frisket Cloud.')).toBeVisible();
  });
});
