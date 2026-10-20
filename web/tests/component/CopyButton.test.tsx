// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { CopyButton } from '../../src/components/CopyButton';

afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

it('copies the prepared text, briefly acknowledges it and resets for a new value', async () => {
  vi.useFakeTimers();
  const writeText = vi.fn().mockResolvedValue(undefined);
  vi.stubGlobal('navigator', { clipboard: { writeText } });
  const view = render(<CopyButton text="one" label="copy ref" />);
  await act(async () => fireEvent.click(screen.getByRole('button')));
  expect(writeText).toHaveBeenCalledWith('one');
  expect(screen.getByRole('button')).toHaveTextContent('copied');
  await act(async () => vi.advanceTimersByTime(1200));
  expect(screen.getByRole('button')).toHaveTextContent('copy ref');
  await act(async () => fireEvent.click(screen.getByRole('button')));
  view.rerender(<CopyButton text="two" label="copy ref" />);
  expect(screen.getByRole('button')).toHaveTextContent('copy ref');
  view.rerender(<CopyButton text="one" label="copy ref" />);
  await act(async () => vi.advanceTimersByTime(1200));
  expect(screen.getByRole('button')).toHaveTextContent('copy ref');
});

it('offers retry on failure and ignores an old value completing after replacement', async () => {
  let resolve!: () => void;
  const writeText = vi.fn().mockRejectedValueOnce(new Error('blocked'))
    .mockImplementationOnce(() => new Promise<void>((done) => { resolve = done; }));
  vi.stubGlobal('navigator', { clipboard: { writeText } });
  const view = render(<CopyButton text="one" />);
  await act(async () => fireEvent.click(screen.getByRole('button')));
  expect(screen.getByRole('button')).toHaveTextContent('retry copy');
  fireEvent.click(screen.getByRole('button'));
  view.rerender(<CopyButton text="two" />);
  await act(async () => resolve());
  expect(screen.getByRole('button')).toHaveTextContent('copy');
  expect(screen.getByRole('button')).not.toHaveTextContent('copied');
});
