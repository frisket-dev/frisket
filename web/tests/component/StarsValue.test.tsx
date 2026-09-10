// @vitest-environment jsdom
//
// A plugin `stars` column resolves to RowDrawer's StarsValue presentation
// renderer. These tests prove the
// star-glyph mapping + raw number the drawer showed for 4.5 and after the edit
// to 3.5. The glyph string is asserted exactly (via the a11y label span) so an
// off-by-one star count can't hide inside a substring match.

import '@testing-library/jest-dom/vitest';
import { cleanup, screen, render } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { StarsValue } from '../../src/components/RowDrawer';



afterEach(cleanup);

describe('StarsValue renderer', () => {
  it('renders a half star for 4.5 with the raw number', () => {
    render(<StarsValue value={4.5} />);
    expect(screen.getByLabelText('4.5 out of 5').textContent).toBe('★★★★½');
    expect(screen.getByTestId('stars-raw-value')).toHaveTextContent('4.5');
  });

  it('re-renders 3.5 as three-and-a-half stars', () => {
    render(<StarsValue value={3.5} />);
    expect(screen.getByLabelText('3.5 out of 5').textContent).toBe('★★★½☆');
    expect(screen.getByTestId('stars-raw-value')).toHaveTextContent('3.5');
  });

  it('renders a whole-number rating with no half star', () => {
    render(<StarsValue value={2} />);
    expect(screen.getByLabelText('2 out of 5').textContent).toBe('★★☆☆☆');
  });
});
