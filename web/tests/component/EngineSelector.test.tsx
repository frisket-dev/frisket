// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { createRef } from 'react';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
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
      renderDetailFooter={({ choice, pinned }) => <button type="button" data-testid="setup-footer">{choice.id}:{String(pinned)}</button>}
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

  it.each(['object', 'callback'] as const)('forwards a %s trigger ref, restores focus, and clears it on unmount', async (kind) => {
    const objectRef = createRef<HTMLButtonElement>();
    const callbackRef = vi.fn<(node: HTMLButtonElement | null) => void>();
    expect(objectRef.current).toBeNull();
    const view = render(<EngineSelector label="Engine" groups={groups} value="parakeet"
      recentNamespace="forwarded-trigger" onSelect={vi.fn()}
      triggerRef={kind === 'object' ? objectRef : callbackRef} />);
    const trigger = screen.getByRole('button', { name: /parakeet/i });
    if (kind === 'object') expect(objectRef.current).toBe(trigger);
    else expect(callbackRef).toHaveBeenLastCalledWith(trigger);
    await userEvent.click(trigger);
    fireEvent(screen.getByTestId('engine-selector-dialog'), new Event('cancel', { cancelable: true }));
    expect(trigger).toHaveFocus();
    view.unmount();
    if (kind === 'object') expect(objectRef.current).toBeNull();
    else expect(callbackRef).toHaveBeenLastCalledWith(null);
  });

  it('resets the search and previews the latest committed selection on reopening', async () => {
    const props = { label: 'Engine', groups, recentNamespace: 'reopening', onSelect: vi.fn() };
    const view = render(<EngineSelector {...props} value="parakeet" />);
    await userEvent.click(screen.getByRole('button', { name: /parakeet/i }));
    await userEvent.type(screen.getByRole('searchbox', { name: 'Search Engine' }), 'Whisper');
    fireEvent(screen.getByTestId('engine-selector-dialog'), new Event('cancel', { cancelable: true }));
    view.rerender(<EngineSelector {...props} value="openai-1" />);
    await userEvent.click(screen.getByRole('button', { name: /openai 1/i }));
    expect(screen.getByRole('searchbox', { name: 'Search Engine' })).toHaveValue('');
    expect(screen.getByRole('heading', { name: 'OpenAI 1' })).toBeVisible();
  });

  it('pins setup-needed details after selecting but keeps the dialog open', async () => {
    const onSelect = renderSelector();
    await userEvent.click(screen.getByRole('button', { name: /parakeet/i }));
    await userEvent.click(screen.getByRole('button', { name: /whisper/i }));

    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ id: 'whisper' }));
    expect(screen.getByTestId('engine-selector-dialog')).toBeVisible();
    expect(screen.getByTestId('setup-footer')).toHaveTextContent('whisper:true');
    await new Promise((resolve) => requestAnimationFrame(resolve));
    expect(screen.getByTestId('setup-footer')).toHaveFocus();
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
    expect(screen.getByTestId('engine-selector-dialog').querySelector('[data-engine-selector-choice="whisper"]')).toHaveFocus();

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

  it.each(['{Enter}', ' '])('opens mobile details before selecting with %s', async (key) => {
    const oldWidth = window.innerWidth;
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 500 });
    try {
      const onSelect = renderSelector();
      await userEvent.click(screen.getByRole('button', { name: /parakeet/i }));
      await new Promise((resolve) => requestAnimationFrame(resolve));
      screen.getByRole('button', { name: /whisper/i }).focus();
      await userEvent.keyboard(key);
      expect(screen.getByTestId('engine-selector-dialog')).toHaveClass('engine-selector__dialog--mobile-detail');
      expect(onSelect).not.toHaveBeenCalled();
      await userEvent.click(screen.getByRole('button', { name: /select and set up/i }));
      expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ id: 'whisper' }));
      expect(screen.getByTestId('setup-footer')).toHaveTextContent('whisper:true');
    } finally {
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: oldWidth });
    }
  });

  it('keeps recent ordering stable while open and refreshes it on the next opening', async () => {
    renderSelector();
    const trigger = screen.getByRole('button', { name: /parakeet/i });
    await userEvent.click(trigger);
    const choiceIds = () => Array.from(screen.getByTestId('engine-selector-dialog').querySelectorAll<HTMLElement>('[data-engine-selector-choice]'))
      .map((choice) => choice.dataset.engineSelectorChoice);
    expect(choiceIds()).toEqual(['parakeet', 'whisper', 'legacy']);
    await userEvent.click(screen.getByRole('button', { name: /whisper/i }));
    expect(JSON.parse(localStorage.getItem('frisket:engine-selector:recent:component-test') ?? '[]')).toEqual(['whisper']);
    expect(choiceIds()).toEqual(['parakeet', 'whisper', 'legacy']);
    fireEvent(screen.getByTestId('engine-selector-dialog'), new Event('cancel', { cancelable: true }));
    await userEvent.click(trigger);
    expect(choiceIds()).toEqual(['whisper', 'parakeet', 'legacy']);
  });

  it('uses searchboxes without native search Escape consumption and cancels with a populated query', async () => {
    const longGroup = { ...groups[1], choices: Array.from({ length: 13 }, (_, index) => ({ ...groups[1].choices[0], id: `hosted-${index}`, label: `Hosted ${index}` })) };
    render(<EngineSelector label="Engine" groups={[longGroup]} value="hosted-0" recentNamespace="escape-search" onSelect={vi.fn()} />);
    const trigger = screen.getByRole('button', { name: /hosted 0/i });
    await userEvent.click(trigger);
    const search = screen.getByRole('searchbox', { name: 'Search Engine' });
    const filter = screen.getByRole('searchbox', { name: 'Filter OpenAI choices' });
    expect(search).toHaveAttribute('type', 'text');
    expect(filter).toHaveAttribute('type', 'text');
    await userEvent.type(search, 'Hosted');
    fireEvent(screen.getByTestId('engine-selector-dialog'), new Event('cancel', { cancelable: true }));
    expect(screen.queryByTestId('engine-selector-dialog')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it('keeps mobile provider tabs and returns focus to the tapped choice from detail', async () => {
    const oldWidth = window.innerWidth;
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 500 });
    renderSelector();
    await userEvent.click(screen.getByRole('button', { name: /parakeet/i }));
    await userEvent.click(screen.getByRole('button', { name: 'OpenAI' }));
    expect(screen.getByRole('button', { name: /openai 0/i })).toBeVisible();
    await userEvent.click(screen.getByRole('button', { name: /openai 0/i }));
    const back = screen.getByRole('button', { name: /back/i });
    await new Promise((resolve) => requestAnimationFrame(resolve));
    expect(back).toHaveFocus();
    await userEvent.click(back);
    await new Promise((resolve) => requestAnimationFrame(resolve));
    expect(screen.getByRole('button', { name: /openai 0/i })).toHaveFocus();
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: oldWidth });
  });

  it('leaves setup editing when explicit provider navigation changes the list', async () => {
    renderSelector();
    await userEvent.click(screen.getByRole('button', { name: /parakeet/i }));
    await userEvent.click(screen.getByRole('button', { name: /whisper/i }));
    await userEvent.click(screen.getByRole('button', { name: 'OpenAI' }));
    const dialog = screen.getByTestId('engine-selector-dialog');
    fireEvent.mouseEnter(dialog.querySelector('[data-engine-selector-choice="openai-1"]')!);
    await new Promise((resolve) => setTimeout(resolve, 150));
    expect(screen.getByRole('heading', { name: 'OpenAI 1' })).toBeVisible();
  });

  it('replaces provider hover intent before a later choice hover can preview', async () => {
    const oldWidth = window.innerWidth;
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1200 });
    renderSelector();
    const trigger = screen.getByRole('button', { name: /parakeet/i });
    vi.spyOn(trigger, 'getBoundingClientRect').mockReturnValue(new DOMRect(600, 100, 300, 40));
    await userEvent.click(trigger);
    await new Promise((resolve) => requestAnimationFrame(resolve));
    const dialog = screen.getByTestId('engine-selector-dialog');
    fireEvent.mouseEnter(dialog.querySelector('[data-engine-selector-group="openai"]')!);
    fireEvent.mouseLeave(dialog.querySelector('[data-engine-selector-group="openai"]')!);
    fireEvent.mouseEnter(dialog.querySelector('[data-engine-selector-choice="whisper"]')!);
    await new Promise((resolve) => setTimeout(resolve, 150));
    expect(screen.getByRole('heading', { name: 'Whisper' })).toBeVisible();
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: oldWidth });
  });

  it('uses the flat body whenever the provider rail is absent, including desktop search and small catalogs', async () => {
    const oldWidth = window.innerWidth;
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1200 });
    try {
      const props = { label: 'Engine', value: 'parakeet', recentNamespace: 'flat-body', onSelect: vi.fn() };
      const view = render(<EngineSelector {...props} groups={groups} />);
      const trigger = screen.getByRole('button', { name: /parakeet/i });
      vi.spyOn(trigger, 'getBoundingClientRect').mockReturnValue(new DOMRect(600, 100, 300, 40));
      await userEvent.click(trigger);
      const body = screen.getByTestId('engine-selector-dialog').querySelector('.engine-selector__body')!;
      expect(body.querySelector('.engine-selector__groups')).toBeInTheDocument();
      expect(body).not.toHaveClass('engine-selector__body--flat');
      await userEvent.type(screen.getByRole('searchbox', { name: 'Search Engine' }), 'hosted');
      expect(body.querySelector('.engine-selector__groups')).not.toBeInTheDocument();
      expect(body.children).toHaveLength(2);
      expect(body).toHaveClass('engine-selector__body--flat');
      await userEvent.clear(screen.getByRole('searchbox', { name: 'Search Engine' }));
      view.rerender(<EngineSelector {...props} groups={groups.map((group) => ({ ...group, choices: group.choices.slice(0, 1) }))} />);
      expect(body.querySelector('.engine-selector__groups')).not.toBeInTheDocument();
      expect(body.children).toHaveLength(2);
      expect(body).toHaveClass('engine-selector__body--flat');
      const providers = screen.getByRole('navigation', { name: 'Providers' });
      await userEvent.click(within(providers).getByRole('button', { name: 'OpenAI' }));
      expect(screen.getByRole('heading', { name: 'OpenAI 0' })).toBeVisible();
    } finally {
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: oldWidth });
    }
  });

  it('portals setup forms and stops their submit from reaching the action form', async () => {
    const outerSubmit = vi.fn();
    render(
      <form onSubmit={outerSubmit}>
        <EngineSelector
          label="Engine"
          groups={groups}
          value="parakeet"
          recentNamespace="outer-form"
          onSelect={vi.fn()}
          renderDetailFooter={() => <form data-testid="setup-form"><button type="submit">Save setup</button></form>}
        />
      </form>,
    );
    await userEvent.click(screen.getByRole('button', { name: /parakeet/i }));
    fireEvent.submit(screen.getByTestId('setup-form'));
    expect(outerSubmit).not.toHaveBeenCalled();
  });
});
