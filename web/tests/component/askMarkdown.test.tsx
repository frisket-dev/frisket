// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, screen, render } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { MarkdownView } from '../../src/markdown';



afterEach(cleanup);

const REPLY =
  '**Plan**\n\n- Add a column\n- Preview five rows\n\n' +
  '[Open docs](https://example.com/docs)\n\n' +
  '<script>window.__askMarkdownXss = 1</script>';

describe('ask assistant markdown rendering', () => {
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
