// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ArtifactSource, TextArtifactSource } from '../../src/components/EvidenceViewer';
import type { EvidenceArtifact } from '../../src/api/types';
import { evidenceArtifact } from '../support/evidenceFixtures';

type TextContext = {
  text: string;
  offset_unit: 'utf16_code_unit';
  ranges: Array<{ span_id: string; start: number; end: number }>;
};

type ArtifactWithTextContext = EvidenceArtifact & { text_context: TextContext | null };

function textArtifact(title: string, text: string, ranges: TextContext['ranges']): ArtifactWithTextContext {
  return {
    ...evidenceArtifact({
      title,
      media_type: 'application/vnd.frisket.row+json',
    }),
    text_context: { text, offset_unit: 'utf16_code_unit', ranges },
  };
}

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn();
});

afterEach(cleanup);

describe('saved evidence text context', () => {
  it('renders every server-supplied occurrence without locating quotes in the browser', () => {
    const text = 'The cited clause appears here. The cited clause appears again.';
    const quote = 'cited clause';
    const first = text.indexOf(quote);
    const second = text.lastIndexOf(quote);
    const artifact = textArtifact('Filing text', text, [
      { span_id: 'claim:1', start: first, end: first + quote.length },
      { span_id: 'claim:1', start: second, end: second + quote.length },
    ]);

    render(<TextArtifactSource artifact={artifact} />);

    const body = screen.getByTestId('evidence-text-body');
    expect(body).toHaveTextContent(text);
    expect(within(body).getAllByTestId('evidence-text-highlight')).toHaveLength(2);
    expect(Element.prototype.scrollIntoView).toHaveBeenCalledTimes(1);
  });

  it('uses UTF-16 offsets so an emoji before a citation does not shift its mark', () => {
    const text = '😀 The named party is Cedar Bridge.';
    const quote = 'Cedar Bridge';
    const start = text.indexOf(quote);
    const artifact = textArtifact('Markdown source', text, [
      { span_id: 'claim:emoji', start, end: start + quote.length },
    ]);

    render(<TextArtifactSource artifact={artifact} />);

    expect(screen.getByTestId('evidence-text-body')).toHaveTextContent(text);
    expect(screen.getByTestId('evidence-text-highlight')).toHaveTextContent(quote);
  });

  it('filters saved ranges to the focused span and clears them when highlighting is disabled', () => {
    const text = 'First passage. Second passage.';
    const first = 'First passage';
    const second = 'Second passage';
    const artifact = textArtifact('Two claims', text, [
      { span_id: 'claim:first', start: text.indexOf(first), end: text.indexOf(first) + first.length },
      { span_id: 'claim:second', start: text.indexOf(second), end: text.indexOf(second) + second.length },
    ]);

    const { rerender } = render(
      <TextArtifactSource artifact={artifact} scopeSpanId="claim:second" />,
    );

    expect(screen.getAllByTestId('evidence-text-highlight')).toHaveLength(1);
    expect(screen.getByTestId('evidence-text-highlight')).toHaveTextContent(second);

    rerender(<TextArtifactSource artifact={artifact} scopeSpanId="claim:second" highlight={false} />);
    expect(screen.queryByTestId('evidence-text-highlight')).not.toBeInTheDocument();
  });

  it('keeps sources separate even when their passages have the same text', () => {
    const quote = 'Shared passage';
    const first = textArtifact('Correspondence', `A ${quote}.`, [
      { span_id: 'claim:letter', start: 2, end: 2 + quote.length },
    ]);
    const second = textArtifact('Filing text', `B ${quote}.`, [
      { span_id: 'claim:filing', start: 2, end: 2 + quote.length },
    ]);

    render(
      <>
        <ArtifactSource artifact={first} />
        <ArtifactSource artifact={second} />
      </>,
    );

    expect(screen.getAllByTestId('evidence-artifact')).toHaveLength(2);
    expect(screen.getByText('Correspondence')).toBeVisible();
    expect(screen.getByText('Filing text')).toBeVisible();
    expect(screen.getAllByTestId('evidence-text-highlight')).toHaveLength(2);
  });
});
