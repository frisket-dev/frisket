// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { OverflowRow } from '../../src/components/OverflowRow';

let available = 180;
const resizeCallbacks = new Set<ResizeObserverCallback>();
beforeEach(() => {
  available = 180;
  vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockImplementation(() => available);
  vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockImplementation(function (this: HTMLElement) {
    const content = this.matches('[data-width]') ? this : this.querySelector<HTMLElement>('[data-width]');
    return content ? Number(content.dataset.width) : 20;
  });
  vi.stubGlobal('ResizeObserver', class {
    constructor(callback: ResizeObserverCallback) { resizeCallbacks.add(callback); }
    observe() {}
    disconnect() {}
  });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); resizeCallbacks.clear(); });

const items = [0, 1, 2];
function Example({ keepVisibleKey, onChoose = () => {}, widths = [80, 80, 80] }: {
  keepVisibleKey?: string; onChoose?(item: number): void; widths?: number[];
}) {
  return <OverflowRow items={items} getKey={String} keepVisibleKey={keepVisibleKey} overflowLabel="More commands"
    renderItem={(item, measuring) => measuring
      ? <span data-width={widths[item]}>Command {item}</span>
      : <button data-width={widths[item]} onClick={() => onChoose(item)}>Command {item}</button>}
    renderOverflowItem={(item, closeMenu) => <button role="menuitem" onClick={() => {
      onChoose(item); closeMenu();
    }}>Run {item}</button>} />;
}
function resize(width: number) {
  available = width;
  act(() => resizeCallbacks.forEach((callback) => callback([], {} as ResizeObserver)));
}

it('accepts arbitrary values and caller-owned dropdown actions without test IDs', () => {
  const choose = vi.fn();
  render(<Example onChoose={choose} />);
  expect(screen.getByRole('button', { name: 'Command 0' })).toBeVisible();
  expect(screen.getByRole('button', { name: 'Command 1' })).toBeVisible();
  expect(screen.queryByRole('button', { name: 'Command 2' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'More commands' }));
  fireEvent.click(screen.getByRole('menuitem', { name: 'Run 2' }));
  expect(choose).toHaveBeenCalledWith(2);
  expect(screen.getByRole('button', { name: 'More commands' })).toHaveFocus();
  expect(screen.queryByRole('menu')).not.toBeInTheDocument();
});

it('keeps the requested item visible when it fits and closes overflow when widened', () => {
  render(<Example keepVisibleKey="2" />);
  expect(screen.getByRole('button', { name: 'Command 2' })).toBeVisible();
  expect(screen.queryByRole('button', { name: 'Command 1' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'More commands' }));
  expect(screen.getByRole('menuitem', { name: 'Run 1' })).toBeVisible();
  resize(400);
  expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'More commands' })).not.toBeInTheDocument();
  resize(180);
  expect(screen.getByRole('button', { name: 'More commands' })).toHaveAttribute('aria-expanded', 'false');
});

it('puts all items in the menu when none fits beside the chevron', () => {
  available = 60;
  render(<Example keepVisibleKey="2" />);
  expect(screen.queryByRole('button', { name: /Command \d/ })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'More commands' }));
  expect(screen.getAllByRole('menuitem')).toHaveLength(3);
});

it('retains fitting items when the requested item is narrower than the first overflowed item', () => {
  render(<Example keepVisibleKey="2" widths={[80, 200, 50]} />);
  expect(screen.getByRole('button', { name: 'Command 0' })).toBeVisible();
  expect(screen.getByRole('button', { name: 'Command 2' })).toBeVisible();
  expect(screen.queryByRole('button', { name: 'Command 1' })).not.toBeInTheDocument();
});

it('moves keyboard focus through caller-rendered menu entries', () => {
  available = 60;
  render(<Example />);
  fireEvent.click(screen.getByRole('button', { name: 'More commands' }));
  expect(screen.getByRole('menuitem', { name: 'Run 0' })).toHaveFocus();
  fireEvent.keyDown(screen.getByRole('menu'), { key: 'ArrowDown' });
  expect(screen.getByRole('menuitem', { name: 'Run 1' })).toHaveFocus();
  fireEvent.keyDown(screen.getByRole('menu'), { key: 'End' });
  expect(screen.getByRole('menuitem', { name: 'Run 2' })).toHaveFocus();
});
