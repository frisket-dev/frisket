// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import { EngineSelector } from '../../src/components/engine-selector';
import type { EngineSelectorGroup } from '../../src/components/engine-selector';
import { installDialogPolyfill } from '../support/domPolyfills';

beforeAll(installDialogPolyfill);
afterEach(() => {
  cleanup();
  localStorage.clear();
});

const groups: EngineSelectorGroup[] = [
  {
    id: 'local',
    label: 'Local',
    choices: [
      { id: 'parakeet', label: 'Parakeet', summary: 'Fast speech recognition', status: 'ready', canAuthor: true, canRun: true, isDefault: true },
      { id: 'whisper', label: 'Whisper', summary: 'Broad language coverage', status: 'needs_setup', canAuthor: true, canRun: false, blocker: 'Download required' },
      { id: 'legacy', label: 'Legacy', summary: 'Saved selection', status: 'unavailable', canAuthor: false, canRun: false, blocker: 'No compatible runtime' },
    ],
  },
  {
    id: 'openai',
    label: 'OpenAI',
    choices: Array.from({ length: 4 }, (_, index) => ({
      id: `openai-${index}`,
      label: `OpenAI ${index}`,
      summary: 'Hosted choice',
      status: 'ready' as const,
      canAuthor: true,
      canRun: true,
    })),
  },
];

function renderSelector(onSelect = vi.fn()) {
  render(
    <EngineSelector
      label="Engine"
      groups={groups}
      value="parakeet"
      recentNamespace="component-test"
      onSelect={onSelect}
      renderDetailFooter={({ choice, pinned }) => <div data-testid="setup-footer">{choice.id}:{String(pinned)}</div>}
    />,
  );
  return onSelect;
}

describe('EngineSelector', () => {
  it('selects a ready choice, records a bounded recent, and restores focus after closing', async () => {
    const onSelect = renderSelector();
    const trigger = screen.getByRole('button', { name: /parakeet/i });
    await userEvent.click(trigger);
    await userEvent.click(screen.getAllByRole('button', { name: 'OpenAI' })[0]);
    await userEvent.click(screen.getByRole('button', { name: /openai 0/i }));

    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ id: 'openai-0' }));
    expect(screen.queryByTestId('engine-selector-dialog')).not.toBeInTheDocument();
    expect(JSON.parse(localStorage.getItem('frisket:engine-selector:recent:component-test') ?? '[]')).toEqual(['openai-0']);
    expect(trigger).toHaveFocus();
  });

  it('pins setup-needed details after selecting but keeps the dialog open', async () => {
    const onSelect = renderSelector();
    await userEvent.click(screen.getByRole('button', { name: /parakeet/i }));
    await userEvent.click(screen.getByRole('button', { name: /whisper/i }));

    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ id: 'whisper' }));
    expect(screen.getByTestId('engine-selector-dialog')).toBeVisible();
    expect(screen.getByTestId('setup-footer')).toHaveTextContent('whisper:true');
  });

  it('only inspects an unavailable choice', async () => {
    const onSelect = renderSelector();
    await userEvent.click(screen.getByRole('button', { name: /parakeet/i }));
    await userEvent.click(screen.getByRole('button', { name: /legacy/i }));

    expect(onSelect).not.toHaveBeenCalled();
    expect(screen.getByTestId('engine-selector-dialog')).toBeVisible();
    expect(screen.getByText('No compatible runtime')).toBeVisible();
  });

  it('searches labels, summaries, and providers without reviving excluded recent ids', async () => {
    localStorage.setItem('frisket:engine-selector:recent:component-test', JSON.stringify(['removed', 'whisper']));
    renderSelector();
    await userEvent.click(screen.getByRole('button', { name: /parakeet/i }));
    await userEvent.type(screen.getByRole('searchbox', { name: 'Search Engine' }), 'hosted');

    expect(screen.getByRole('button', { name: /openai 0/i })).toBeVisible();
    expect(screen.getByTestId('engine-selector-dialog').querySelector('[data-engine-selector-choice="parakeet"]')).not.toBeInTheDocument();
    expect(screen.queryByText('Recent')).not.toBeInTheDocument();
  });

  it('keeps choice keyboard navigation on ordinary buttons and returns focus on dialog cancel', async () => {
    renderSelector();
    const trigger = screen.getByRole('button', { name: /parakeet/i });
    await userEvent.click(trigger);
    await new Promise((resolve) => requestAnimationFrame(resolve));
    const parakeet = screen.getByTestId('engine-selector-dialog').querySelector<HTMLButtonElement>('[data-engine-selector-choice="parakeet"]')!;
    parakeet.focus();
    await userEvent.keyboard('{ArrowDown}');
    expect(screen.getByRole('button', { name: /whisper/i })).toHaveFocus();

    fireEvent(screen.getByTestId('engine-selector-dialog'), new Event('cancel', { cancelable: true }));
    expect(screen.queryByTestId('engine-selector-dialog')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it('uses a detail sheet with an explicit Select control below 600px', async () => {
    const oldWidth = window.innerWidth;
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 500 });
    const onSelect = renderSelector();
    await userEvent.click(screen.getByRole('button', { name: /parakeet/i }));
    await userEvent.click(screen.getByRole('button', { name: /whisper/i }));

    expect(screen.getByRole('button', { name: /back/i })).toBeVisible();
    await userEvent.click(screen.getByRole('button', { name: /select and set up/i }));
    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ id: 'whisper' }));
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: oldWidth });
  });
});
