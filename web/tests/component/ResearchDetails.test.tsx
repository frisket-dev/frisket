// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import type { RunTraceRow, RunTraceRowEvidence } from '../../src/api/types';
import { ModelCallDetails, ResearchActivitySummary, ResearchDetails, SourceLinks } from '../../src/components/research/ResearchDetails';

const originalShowModal = Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype, 'showModal');
const originalClose = Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype, 'close');
beforeAll(() => {
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, value(this: HTMLDialogElement) { this.setAttribute('open', ''); } });
  Object.defineProperty(HTMLDialogElement.prototype, 'close', { configurable: true, value(this: HTMLDialogElement) { this.removeAttribute('open'); } });
});
afterAll(() => {
  if (originalShowModal) Object.defineProperty(HTMLDialogElement.prototype, 'showModal', originalShowModal);
  else delete (HTMLDialogElement.prototype as Partial<HTMLDialogElement>).showModal;
  if (originalClose) Object.defineProperty(HTMLDialogElement.prototype, 'close', originalClose);
  else delete (HTMLDialogElement.prototype as Partial<HTMLDialogElement>).close;
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

function evidence(overrides: Partial<RunTraceRow> = {}): RunTraceRowEvidence {
  return {
    runId: 'run-1', rowId: 'row-1', columnId: 'answer-column', recorded: true, status: 'recorded',
    cell: { value: 'The exact-run answer.', error: null },
    run: { actionKind: 'research.answer', actionName: 'Agent Research', status: 'completed', model: 'test/model', startedAt: null, finishedAt: null, costActual: null },
    trace: {
      runId: 'run-1', traceId: 'trace-1', actionKind: 'research.answer', actionName: 'Agent Research', model: 'test/model', createdAt: null, recordCount: 1,
      row: { rowId: 'row-1', prompt: null, rawResponse: 'A different intermediate response.', data: null, error: null, tokensIn: 10, tokensOut: 5, cost: null, cached: false, latencyMs: 42, retries: 0, ...overrides },
    },
  };
}

describe('research evidence presentation', () => {
  it('shows ordered actual tool observations, returned text, services and explicit truncation', () => {
    render(<ResearchDetails evidence={evidence({ calls: [{ model: 'test/model' }], research: {
      truncated: true, steps: [
        { tool: 'search', query: 'synthetic city records', provider: 'ddgs', service: 'duckduckgo', status: 'ok', results: [{ title: 'City records', url: 'https://example.test/records', snippet: 'A returned snippet.' }], duration_ms: 3, truncated: false },
        { tool: 'fetch', url: 'https://example.test/records', status: 'ok', text: 'The text supplied to the model.', duration_ms: 8, truncated: true },
        { tool: 'fetch', url: 'https://example.test/failed', status: 'error', error_code: 'fetch_failed', error: 'Synthetic page unavailable.', text: null, duration_ms: 4, truncated: false },
      ],
    } })} sources={['https://example.test/records']} onClose={vi.fn()} />);
    expect(screen.getByRole('dialog')).toHaveAttribute('data-size', 'wide');
    expect(screen.getByText(/1 model call recorded · 1 search · 1 page read/)).toHaveTextContent('ddgs, duckduckgo');
    expect(screen.getAllByRole('heading', { level: 4 }).map(node => node.textContent).filter(value => /^\d/.test(value ?? '')))
      .toEqual(['1. Search · Succeeded', '2. Read page · Succeeded', '3. Read page · Error']);
    expect(screen.getByText('A returned snippet.')).toBeInTheDocument();
    expect(screen.getByText('The text supplied to the model.')).toBeInTheDocument();
    expect(screen.getByText('Synthetic page unavailable.')).toBeVisible();
    expect(screen.getByText(/This tool result was truncated/)).toBeVisible();
    expect(screen.getByText(/Recorded details were truncated/)).toBeVisible();
    expect(screen.queryByText(/provider-native/i)).not.toBeInTheDocument();
  });

  it('uses exact-run cell errors even when the run completed and shows its final value', () => {
    const item = evidence();
    item.cell = { value: 'A partial exact-run answer.', error: 'Research exhausted its budget.' };
    render(<ResearchDetails evidence={item} onClose={vi.fn()} />);
    expect(screen.getByText('Failed · test/model · 42 ms')).toBeVisible();
    expect(screen.getByText('Research exhausted its budget.')).toBeVisible();
    expect(screen.getByText('A partial exact-run answer.')).toBeVisible();
    expect(screen.queryByText('A different intermediate response.')).not.toBeInTheDocument();
    expect(screen.queryByText('Answer found')).not.toBeInTheDocument();
  });

  it('distinguishes unavailable activity from a recorded run that used no tools', () => {
    const view = render(<ResearchActivitySummary evidence={evidence()} />);
    expect(screen.getByText('Tool activity was not recorded for this run.')).toBeVisible();
    expect(screen.queryByText(/0 searches/)).not.toBeInTheDocument();
    view.rerender(<ResearchActivitySummary evidence={evidence({ calls: [], research: { steps: [], truncated: false }, researchIncomplete: true })} />);
    expect(screen.getByText('0 model calls recorded · 0 searches · 0 pages read')).toBeVisible();
    expect(screen.queryByText('Tool activity was not recorded for this run.')).not.toBeInTheDocument();
    expect(screen.getByText(/Available activity may be incomplete/)).toBeVisible();
  });

  it('does not describe discarded activity as unused tools', () => {
    const view = render(<ResearchDetails evidence={evidence({ research: { steps: [], truncated: false }, researchIncomplete: true })} onClose={vi.fn()} />);
    expect(screen.getByText('No tool steps are available in the recorded details.')).toBeVisible();
    expect(screen.queryByText('No research tools were used.')).not.toBeInTheDocument();
    view.rerender(<ResearchDetails evidence={evidence({ research: { steps: [], truncated: false } })} onClose={vi.fn()} />);
    expect(screen.getByText('No research tools were used.')).toBeVisible();
  });

  it('reports a finished exact-run cell even when another row made the run fail', () => {
    const item = evidence();
    item.run = { ...item.run!, status: 'failed' };
    render(<ResearchDetails evidence={item} onClose={vi.fn()} />);
    expect(screen.getByText('Finished · test/model · 42 ms')).toBeVisible();
  });

  it('keeps older prompt/response evidence and handles an entirely absent trace', () => {
    const view = render(<ResearchDetails evidence={evidence({ prompt: [{ role: 'user', content: 'Old captured prompt' }], rawResponse: 'Old captured response' })} onClose={vi.fn()} />);
    expect(screen.getByText('Recorded model summary').closest('details')).not.toHaveAttribute('open');
    expect(screen.getByText(/Old captured prompt/)).toBeInTheDocument();
    expect(screen.getByText('Old captured response')).toBeInTheDocument();
    view.rerender(<ResearchDetails evidence={{ ...evidence(), recorded: false, status: 'not_recorded', trace: null, cell: null }} onClose={vi.fn()} />);
    expect(screen.getByText('Tool activity was not recorded for this run.')).toBeVisible();
    expect(screen.getByText('No final answer recorded for this run.')).toBeVisible();
    expect(screen.queryByRole('region', { name: 'Sources encountered' })).not.toBeInTheDocument();
  });

  it('renders captured HTML and unsafe source URLs as text', () => {
    const view = render(<SourceLinks value={['https://example.test/safe', 'javascript:alert(1)', 'data:text/html,<h1>bad</h1>']} />);
    expect(screen.getAllByRole('link')).toHaveLength(1);
    expect(screen.getByRole('link')).toHaveAttribute('href', 'https://example.test/safe');
    expect(screen.getByText('javascript:alert(1)')).toBeVisible();
    expect(screen.getByText('data:text/html,<h1>bad</h1>')).toBeVisible();
    view.rerender(<ResearchDetails evidence={evidence({ research: { steps: [{ tool: 'fetch', url: 'javascript:alert(2)', status: 'ok', text: '<img src=x onerror=alert(1)>', duration_ms: null, truncated: false }], truncated: false } })} onClose={vi.fn()} />);
    expect(screen.getByText('<img src=x onerror=alert(1)>')).toBeInTheDocument();
    expect(view.container.querySelector('img')).toBeNull();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it('retains individual requests, tool calls, errors, tokens and copy controls under collapsed calls', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    const request = { model: 'synthetic/model', messages: [{ role: 'user', content: '<b>Plain question</b>' }], tools: [{ name: 'search' }], schema: null, max_tokens: 100, temperature: 0, params: { seed: 1 }, mechanism: 'tools', reasoning_policy: 'default' };
    render(<ModelCallDetails calls={[{ model: 'synthetic/model', request, raw_response: 'Ordinary response', tool_calls: [{ name: 'search', id: 'tool-1', args: { query: 'request evidence' } }], tokens_in: 12, tokens_out: 4, latency_ms: 9, error: 'Synthetic model failure', truncated: true }]} />);
    expect(screen.getByText('Model calls (1)').closest('details')).not.toHaveAttribute('open');
    expect(screen.getByText('Call 1 · synthetic/model · Error')).toBeInTheDocument();
    expect(screen.getByText('12 input tokens · 4 output tokens · 9 ms')).toBeInTheDocument();
    expect(screen.getByLabelText('Request')).toHaveTextContent('reasoning_policy');
    expect(screen.getByLabelText('Tool calls')).toHaveTextContent('tool-1');
    expect(screen.getByLabelText('Error')).toHaveTextContent('Synthetic model failure');
    expect(screen.getByText(/This model call was truncated/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Copy request', hidden: true }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(JSON.stringify(request, null, 2)));
  });

  it('uses the shared dialog dismissal lifecycle and preserves content across focus changes', () => {
    const close = vi.fn();
    const ancestorCancel = vi.fn();
    render(<div onCancel={ancestorCancel}><ResearchDetails evidence={evidence()} onClose={close} /></div>);
    const dialog = screen.getByRole('dialog');
    fireEvent.blur(dialog);
    expect(dialog).toBeVisible();
    fireEvent(dialog, new Event('cancel', { bubbles: true, cancelable: true }));
    expect(close).toHaveBeenCalledTimes(1);
    expect(ancestorCancel).not.toHaveBeenCalled();
  });
});
