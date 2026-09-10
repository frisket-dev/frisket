// @vitest-environment jsdom
//
// A Copilot reply can carry markdown plus an inline <script>; the rendered
// assistant bubble must show sanitized markdown and never execute the XSS.
// That bubble is MarkdownView (CopilotPanel renders it with
// testId="copilot-response-markdown", decodeEscapes) — the copilot send/receive
// wiring is incidental, so these tests render MarkdownView with the same props
// and untrusted source.

import '@testing-library/jest-dom/vitest';
import { cleanup, screen, render } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { MarkdownView } from '../../src/markdown';



afterEach(cleanup);

const REPLY =
  '**Plan**\n\n- Add a column\n- Preview five rows\n\n' +
  '[Open docs](https://example.com/docs)\n\n' +
  '<script>window.__copilotMarkdownXss = 1</script>';

describe('copilot assistant markdown rendering', () => {
  it('renders sanitized markdown and never executes embedded script', () => {
    delete (window as unknown as { __copilotMarkdownXss?: number }).__copilotMarkdownXss;
    render(
      <MarkdownView
        className="copilot-bubble copilot-markdown"
        source={REPLY}
        testId="copilot-response-markdown"
        decodeEscapes
      />,
    );

    const rendered = screen.getByTestId('copilot-response-markdown');
    expect(rendered.querySelector('strong')).toHaveTextContent('Plan');
    expect(rendered.querySelectorAll('li')).toHaveLength(2);
    expect(rendered.querySelector('a')).toHaveAttribute('href', 'https://example.com/docs');
    expect(rendered.querySelectorAll('script')).toHaveLength(0);
    expect(
      (window as unknown as { __copilotMarkdownXss?: number }).__copilotMarkdownXss,
    ).toBeUndefined();
  });
});
