// @vitest-environment jsdom
//
// FieldValue decodes escaped AI text without changing raw text, sanitizes
// HTML-like text, and expands large numbers without scientific notation. These
// tests mount the renderer directly because import and drawer navigation do not
// affect those contracts.

import '@testing-library/jest-dom/vitest';
import { cleanup, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { FieldValue } from '../../src/components/RowDrawer';
import { createProjectApi } from '../../src/api/real';
import { aiMeta, columnDef, row } from '../support/domainFixtures';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const projectApi = createProjectApi('field-value');
const { render } = createWorkspaceTestHarness({
  projectId: 'field-value',
  api: { projectApi: projectApi },
});

afterEach(cleanup);

describe('FieldValue renderer', () => {
  it('decodes literal escaped newline and unicode sequences for AI text columns', () => {
    const col = columnDef({ id: 'summary', name: 'summary', type: 'text', ai: aiMeta() });
    const value = 'First line\\nSecond line \\u2014 dash';
    render(<FieldValue col={col} columns={[col]} row={row({ summary: value })} value={value} />);

    const el = screen.getByText(/First line/);
    expect(el).toHaveTextContent('First line');
    expect(el).toHaveTextContent('Second line — dash');
    // the escaped literals must be gone, not shown verbatim
    expect(el.textContent).not.toContain('\\u2014');
    expect(el.textContent).not.toContain('\\n');
  });

  it('leaves a non-AI column\'s literal backslash path untouched', () => {
    const col = columnDef({ id: 'path', name: 'path', type: 'text' });
    const value = 'C:\\new_folder\\notes.txt';
    render(<FieldValue col={col} columns={[col]} row={row({ path: value })} value={value} />);
    expect(screen.getByText(/notes\.txt/)).toHaveTextContent('C:\\new_folder\\notes.txt');
  });

  it('renders HTML-ish text safely and keeps the raw source available', () => {
    const col = columnDef({ id: 'description', name: 'description', type: 'text' });
    const value =
      '<p>Hello <strong>world</strong></p><script>alert(1)</script>' +
      '<a href="javascript:alert(1)">bad</a>';
    const { container } = render(
      <FieldValue col={col} columns={[col]} row={row({ description: value })} value={value} />,
    );

    const html = screen.getByTestId('html-value');
    expect(html).toHaveTextContent('Hello world');
    expect(html.querySelectorAll('script')).toHaveLength(0);
    expect(html.querySelectorAll('a[href^="javascript:"]')).toHaveLength(0);

    // the unsanitized source is still recoverable in the collapsed "Raw" panel
    const raw = container.querySelector('.row-field-raw pre');
    expect(raw?.textContent).toContain('<script>alert(1)</script>');
  });

  it('treats plain_text email bodies as literal text even when they look like HTML', () => {
    const col = columnDef({ id: 'body', name: 'body', type: 'text', format: 'plain_text' });
    const value = '<body>Hello <a href="https://evil.example">link</a></body><img src="x"><style>p{display:none}</style>';
    const { container } = render(
      <FieldValue col={col} columns={[col]} row={row({ body: value })} value={value} />,
    );

    expect(container.querySelector('.row-field-value')).toHaveTextContent(value);
    expect(screen.queryByTestId('html-value')).toBeNull();
    expect(container.querySelectorAll('img, style, a')).toHaveLength(0);
  });

  it('renders canonical email attachment envelopes as filename-only blob downloads', () => {
    const col = columnDef({ id: 'attachments', name: 'attachments', type: 'json' });
    const first = 'a'.repeat(64);
    const second = 'b'.repeat(64);
    const value = JSON.stringify([
      { blob: first, mime: 'application/pdf', filename: 'invoice.pdf' },
      { blob: second, mime: 'image/png', filename: 'diagram.png' },
    ]);
    const { container } = render(
      <FieldValue col={col} columns={[col]} row={row({ attachments: value })} value={value} />,
    );

    const links = [...container.querySelectorAll<HTMLAnchorElement>('a')];
    expect(links.map((link) => link.textContent)).toEqual(['invoice.pdf', 'diagram.png']);
    expect(links.map((link) => link.getAttribute('href'))).toEqual([
      `/api/projects/field-value/blobs/${first}`,
      `/api/projects/field-value/blobs/${second}`,
    ]);
    for (const link of links) {
      expect(link).toHaveAttribute('download');
      expect(link).not.toHaveAttribute('target');
    }
    expect(container).not.toHaveTextContent(first);
    expect(container).not.toHaveTextContent('application/pdf');
  });

  it('keeps malformed or mixed attachment JSON on the generic renderer path', () => {
    const col = columnDef({ id: 'attachments', name: 'attachments', type: 'json' });
    const malformed = '[{"blob":"not-a-digest","filename":"bad.pdf"}]';
    const mixed = JSON.stringify([
      { blob: 'c'.repeat(64), mime: 'application/pdf', filename: 'good.pdf' },
      { blob: 'not-a-digest', mime: 'application/pdf', filename: 'bad.pdf' },
    ]);

    const { container, rerender } = render(
      <FieldValue col={col} columns={[col]} row={row({ attachments: malformed })} value={malformed} />,
    );
    expect(container.querySelector('[data-testid="json-mini-table"]')).toBeTruthy();
    expect(container.querySelector('a[download]')).toBeNull();

    rerender(
      <FieldValue col={col} columns={[col]} row={row({ attachments: mixed })} value={mixed} />,
    );
    expect(container.querySelector('[data-testid="json-mini-table"]')).toBeTruthy();
    expect(container.querySelector('a[download]')).toBeNull();
  });

  it('never previews a malformed image attachment through the generic fallback', () => {
    const col = columnDef({ id: 'attachments', name: 'attachments', type: 'json' });
    const malformed = JSON.stringify([{
      blob: `A${'a'.repeat(63)}`,
      mime: 'image/png',
      filename: 'untrusted.png',
      producer_extension: true,
    }]);
    const { container } = render(
      <FieldValue col={col} columns={[col]} row={row({ attachments: malformed })} value={malformed} />,
    );

    expect(container.querySelector('[data-testid="json-mini-table"]')).toBeTruthy();
    expect(container.querySelector('[data-testid="json-blob-thumb"]')).toBeNull();
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('a[download]')).toBeNull();
  });

  it('shows an explicit "(empty)" marker for a real null value', () => {
    const col = columnDef({ id: 'head_of_state', name: 'head_of_state', type: 'text' });
    const { container } = render(
      <FieldValue col={col} columns={[col]} row={row({ head_of_state: null })} value={null} />,
    );
    const empty = container.querySelector('.row-field-empty');
    expect(empty).not.toBeNull();
    expect(empty).toHaveTextContent('(empty)');
    // never the literal string "null" as if it were text
    expect(empty?.textContent).not.toBe('null');
  });

  it('shows "(empty)" for an empty string too', () => {
    const col = columnDef({ id: 'note', name: 'note', type: 'text' });
    const { container } = render(
      <FieldValue col={col} columns={[col]} row={row({ note: '' })} value={''} />,
    );
    expect(container.querySelector('.row-field-empty')).toHaveTextContent('(empty)');
  });

  it('expands large numbers to grouped digits instead of scientific notation', () => {
    const col = columnDef({ id: 'value', name: 'value', type: 'number' });
    const value = 3.3333333333333335e71;
    const { container } = render(
      <FieldValue col={col} columns={[col]} row={row({ value })} value={value} />,
    );
    const text = container.querySelector('.row-field-value')?.textContent ?? '';
    expect(text).not.toMatch(/e\+?71/i);
    expect(text).toMatch(/333[,0-9]{20,}/);
  });

  it('shows source-bound timestamp details instead of opaque JSON', () => {
    const col = columnDef({ id: 'cuts', name: 'cuts', type: 'timeline_points' });
    const value = JSON.stringify({
      schema_version: 'frisket.timeline_points.v1',
      timeline: {
        artifact_stable_id: 'source_artifact:interview-video',
        fingerprint: `sha256:${'a'.repeat(64)}`,
        duration_ms: 90_000,
      },
      items: [
        { id: 'opening', at_ms: 12_500, label: 'Opening claim' },
        { id: 'response', at_ms: 45_000 },
      ],
    });
    render(<FieldValue col={col} columns={[col]} row={row({ cuts: value })} value={value} />);

    expect(screen.getByTestId('temporal-value')).toHaveTextContent('Source timeline');
    expect(screen.getByTestId('temporal-value')).toHaveTextContent('1:30 total');
    expect(screen.getByTestId('temporal-value')).toHaveTextContent('0:12.500');
    expect(screen.getByTestId('temporal-value')).toHaveTextContent('Opening claim');
    expect(screen.getByTestId('temporal-value')).not.toHaveTextContent('schema_version');
  });

  it('shows only text and type for entity-mention json, hiding offsets/score/fingerprint', () => {
    const col = columnDef({ id: 'entities', name: 'entities', type: 'json' });
    const value = JSON.stringify([
      { text: 'Ada Lovelace', type: 'PERSON', start: 0, end: 12, score: 0.98, fingerprint: 'person:ada lovelace' },
      { text: 'London', type: 'GPE', start: 20, end: 26, score: 0.91, fingerprint: 'gpe:london' },
    ]);
    render(<FieldValue col={col} columns={[col]} row={row({ entities: value })} value={value} />);

    const table = screen.getByTestId('entity-mini-table');
    const headers = [...table.querySelectorAll('th')].map((th) => th.textContent);
    expect(headers).toEqual(['text', 'type']);
    expect(table).toHaveTextContent('Ada Lovelace');
    expect(table).toHaveTextContent('PERSON');
    // plumbing columns are gone from both header and body
    expect(table).not.toHaveTextContent('fingerprint');
    expect(table).not.toHaveTextContent('0.98');
    expect(table).not.toHaveTextContent('person:ada lovelace');
  });

  it('hides the redundant segment_index column for timestamped transcript segments', () => {
    const col = columnDef({ id: 'segments', name: 'segments', type: 'json' });
    const value = JSON.stringify([
      { segment_index: 0, start: 0.0, end: 3.2, text: 'Good morning.' },
      { segment_index: 1, start: 3.2, end: 6.4, text: 'Thanks for having me.' },
    ]);
    render(<FieldValue col={col} columns={[col]} row={row({ segments: value })} value={value} />);

    const table = screen.getByTestId('transcript-segment-mini-table');
    const headers = [...table.querySelectorAll('th')].map((th) => th.textContent);
    expect(headers).toEqual(['start', 'end', 'text']);
    expect(headers).not.toContain('segment_index');
    expect(table).toHaveTextContent('Good morning.');
  });

  it('leaves a generic {text,type} json list untouched (not treated as entities)', () => {
    const col = columnDef({ id: 'tags', name: 'tags', type: 'json' });
    const value = JSON.stringify([{ text: 'urgent', type: 'flag' }]);
    render(<FieldValue col={col} columns={[col]} row={row({ tags: value })} value={value} />);
    // No entity-specific offset/score/fingerprint key → the ordinary mini table.
    expect(screen.getByTestId('json-mini-table')).toBeInTheDocument();
    expect(screen.queryByTestId('entity-mini-table')).toBeNull();
  });

  it('shows labeled source-bound ranges in row detail', () => {
    const col = columnDef({ id: 'excerpt', name: 'excerpt', type: 'timeline_range' });
    const value = JSON.stringify({
      schema_version: 'frisket.timeline_range.v1',
      timeline: {
        artifact_stable_id: 'source_artifact:interview-video',
        fingerprint: `sha256:${'b'.repeat(64)}`,
      },
      item: { id: 'quote', start_ms: 5_000, end_ms: 17_250, label: 'Key quote' },
    });
    render(<FieldValue col={col} columns={[col]} row={row({ excerpt: value })} value={value} />);

    expect(screen.getByTestId('temporal-value')).toHaveTextContent('0:05 – 0:17.250');
    expect(screen.getByTestId('temporal-value')).toHaveTextContent('Key quote');
  });
});
