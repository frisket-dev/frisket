// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, screen, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import DOMPurify from 'dompurify';

import { MarkdownView } from '../../src/markdown';



afterEach(() => { cleanup(); vi.restoreAllMocks(); });

const REPLY =
  '**Plan**\n\n- Add a column\n- Preview five rows\n\n' +
  '[Open docs](https://example.com/docs)\n\n' +
  '<script>window.__askMarkdownXss = 1</script>';

describe('ask assistant markdown rendering', () => {
  it('updates link controls without re-sanitizing unchanged answer text', () => {
    const sanitize = vi.spyOn(DOMPurify, 'sanitize');
    const source = '[Source](#cite-1)';
    const { rerender } = render(<MarkdownView source={source} renderLink={() => <button>First source</button>} />);
    expect(sanitize).toHaveBeenCalledTimes(1);
    rerender(<MarkdownView source={source} renderLink={() => <button>Updated source</button>} />);
    expect(screen.getByRole('button', { name: 'Updated source' })).toBeVisible();
    expect(sanitize).toHaveBeenCalledTimes(1);
    rerender(<MarkdownView source="Changed answer" />);
    expect(sanitize).toHaveBeenCalledTimes(2);
    expect(screen.getByText('Changed answer')).toBeVisible();
  });

  it('renders sanitized markdown and never executes embedded script', () => {
    delete (window as unknown as { __askMarkdownXss?: number }).__askMarkdownXss;
    render(
      <MarkdownView
        className="ask-bubble ask-markdown"
        source={REPLY}
        testId="ask-response-markdown"
      />,
    );

    const rendered = screen.getByTestId('ask-response-markdown');
    expect(rendered.querySelector('strong')).toHaveTextContent('Plan');
    expect(rendered.querySelectorAll('li')).toHaveLength(2);
    expect(rendered.querySelector('a')).toHaveAttribute('href', 'https://example.com/docs');
    expect(rendered.querySelectorAll('script')).toHaveLength(0);
    expect(
      (window as unknown as { __askMarkdownXss?: number }).__askMarkdownXss,
    ).toBeUndefined();
  });
});
