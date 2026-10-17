// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { TextFileViewer } from '../../src/workbench/TextFileViewer';
import { BLOB_TEXT_SECTION_BYTES } from '../../src/api/raw/blobText';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('original text file sections', () => {
  it('navigates to the tail with section controls and keeps full copy explicit and download exact', async () => {
    const text = 'start🙂' + 'x'.repeat(40000) + 'END漢';
    const bytes = new TextEncoder().encode(text);
    const fetch = vi.fn(async (_url: string, options: RequestInit) => {
      if (!options.headers) return new Response(text);
      const range = /^bytes=(\d+)-(\d+)$/.exec((options.headers as { Range: string }).Range)!;
      const start = Number(range[1]);
      const end = Math.min(Number(range[2]), bytes.length - 1);
      return new Response(bytes.slice(start, end + 1), { status: 206,
        headers: { 'Content-Range': `bytes ${start}-${end}/${bytes.length}` } });
    });
    vi.stubGlobal('fetch', fetch);
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('navigator', { clipboard: { writeText } });
    render(<TextFileViewer url="/original" label="notes.txt" />);
    await screen.findByText('Section 1 of 3 · plain text');
    expect(screen.getByTestId('bounded-text-window').textContent!.length).toBeLessThanOrEqual(BLOB_TEXT_SECTION_BYTES);
    expect(screen.queryByLabelText('Go to character')).not.toBeInTheDocument();
    expect(screen.queryByText(/Read-only above/)).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Download full file' })).toHaveAttribute('href', '/original');
    expect(fetch).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Last' }));
    await screen.findByText('Section 3 of 3 · plain text');
    expect(screen.getByTestId('bounded-text-window')).toHaveTextContent('END漢');
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Previous' }));
    await screen.findByText('Section 2 of 3 · plain text');
    fireEvent.click(screen.getByRole('button', { name: 'First' }));
    await screen.findByText('Section 1 of 3 · plain text');
    expect(writeText).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: /Copy full file/ }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(text));
    expect(fetch).toHaveBeenLastCalledWith('/original', { signal: expect.any(AbortSignal) });
  });

  it('aborts an old file read on navigation and never displays its late response', async () => {
    let oldSignal: AbortSignal | undefined;
    let resolveOld!: (response: Response) => void;
    vi.stubGlobal('fetch', vi.fn((url: string, options: RequestInit) => {
      if (url === '/old') {
        oldSignal = options.signal as AbortSignal;
        return new Promise<Response>((resolve) => { resolveOld = resolve; });
      }
      return Promise.resolve(new Response('new', { status: 206, headers: { 'Content-Range': 'bytes 0-2/3' } }));
    }));
    const view = render(<TextFileViewer url="/old" label="old.txt" />);
    view.rerender(<TextFileViewer url="/new" label="new.txt" />);
    expect(oldSignal?.aborted).toBe(true);
    await screen.findByText('new', { selector: 'pre' });
    resolveOld(new Response('old', { status: 206, headers: { 'Content-Range': 'bytes 0-2/3' } }));
    await waitFor(() => expect(screen.getByTestId('bounded-text-window')).toHaveTextContent('new'));
    expect(screen.queryByRole('button', { name: 'Next' })).not.toBeInTheDocument();
  });
});
