// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { BoundedTextReader, type BoundedTextReaderProps } from '../../src/components/BoundedTextReader';

const length = 100_000;
function windowAt(start: number, revision = 9) {
  const end = Math.min(length, start + 16_384);
  return { retrieval_revision: revision, total_characters: length, total_bytes: length + 1,
    range: { start, end }, text: `Text at ${start}: 😀 <strong>literal</strong>`,
    previous_start: start ? Math.max(0, start - 16_384) : null,
    next_start: end < length ? end : null };
}
function props(): BoundedTextReaderProps {
  return { initialRevision: 9, totalCharacters: length, totalBytes: length + 1,
    loadWindow: vi.fn(async ({ start }) => windowAt(start)),
    downloadUrl: vi.fn((revision) => `/exact?revision=${revision}`),
    copyFullValue: vi.fn(async () => 'FULL 😀 VALUE\u0000 tail') };
}
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('bounded text reader', () => {
  it('navigates revision-pinned first, middle and tail windows without interpreting markup', async () => {
    const p = props();
    render(<BoundedTextReader {...p} />);
    await screen.findByText('Text at 0: 😀 <strong>literal</strong>');
    expect(screen.queryByText('literal', { selector: 'strong' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Middle' }));
    await screen.findByText('Text at 50000: 😀 <strong>literal</strong>');
    expect(p.loadWindow).toHaveBeenLastCalledWith(expect.objectContaining({ start: 50_000, revision: 9, limit: 16_384 }));
    fireEvent.click(screen.getByRole('button', { name: 'Last' }));
    await screen.findByText('Text at 83616: 😀 <strong>literal</strong>');
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'First' }));
    await screen.findByText('Text at 0: 😀 <strong>literal</strong>');
  });

  it('opens and highlights a revision-pinned Unicode search passage without marking coarse locators', async () => {
    const p = props();
    const load = vi.fn(async () => ({ ...windowAt(49744), text: `${'x'.repeat(255)}😀needle tail` }));
    const view = render(<BoundedTextReader {...p} initialRange={{ start: 50000, end: 50006 }} loadWindow={load} />);
    expect(await screen.findByTestId('bounded-text-match')).toHaveTextContent(/^needle$/);
    expect(load).toHaveBeenCalledWith(expect.objectContaining({ start: 49744, limit: 16384, revision: 9 }));
    view.unmount();
    render(<BoundedTextReader {...p} initialRange={{ start: 50000, end: 50006, highlight: false }} loadWindow={load} />);
    await screen.findByTestId('bounded-text-window');
    expect(screen.queryByTestId('bounded-text-match')).not.toBeInTheDocument();
  });

  it('makes full copy deliberate and keeps download pinned to the displayed revision', async () => {
    const p = props();
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('navigator', { clipboard: { writeText } });
    render(<BoundedTextReader {...p} />);
    const download = await screen.findByRole('link', { name: 'Download full value' });
    expect(download).toHaveAttribute('href', '/exact?revision=9');
    expect(p.copyFullValue).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: /Copy full value/ }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('FULL 😀 VALUE\u0000 tail'));
    expect(p.copyFullValue).toHaveBeenCalledWith(9, expect.any(AbortSignal));
    expect(writeText).not.toHaveBeenCalledWith(expect.stringContaining('Text at'));
  });

  it('rejects stale windows and reloads the current revision before continuing', async () => {
    const p = props();
    const load = vi.fn().mockRejectedValueOnce(Object.assign(new Error('changed'), { status: 409 }))
      .mockResolvedValueOnce(windowAt(0, 10));
    render(<BoundedTextReader {...p} initialRange={{ start: 50000, end: 50006 }} loadWindow={load} />);
    expect(load).toHaveBeenCalledWith(expect.objectContaining({ start: 49744, revision: 9 }));
    await screen.findByText('This value changed. Reload to read its current version.');
    expect(screen.queryByRole('link', { name: 'Download full value' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Reload text' }));
    await screen.findByTestId('bounded-text-window');
    expect(load).toHaveBeenLastCalledWith(expect.objectContaining({ start: 0, revision: undefined }));
    expect(screen.queryByTestId('bounded-text-match')).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Download full value' })).toHaveAttribute('href', '/exact?revision=10');
  });

  it('aborts a superseded reader window on unmount', async () => {
    let signal: AbortSignal | undefined;
    const load = vi.fn(({ signal: incoming }) => { signal = incoming; return new Promise<never>(() => {}); });
    const { unmount } = render(<BoundedTextReader {...props()} loadWindow={load} />);
    expect(signal?.aborted).toBe(false);
    unmount();
    expect(signal?.aborted).toBe(true);
  });
});
