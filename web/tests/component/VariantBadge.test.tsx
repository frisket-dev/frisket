// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import {
  variantBadgeHeaderIcons,
  variantBadgeIconName,
} from '../../src/workbench/variantBadgePresentation';
import { VariantBadge } from '../../src/workbench/VariantBadge';

afterEach(cleanup);

describe('VariantBadge', () => {
  it('shares the same boxed variant identity with canvas headers', () => {
    render(<VariantBadge label="C" aria-label="Variant C" data-testid="variant-badge" />);

    expect(screen.getByTestId('variant-badge')).toHaveClass('variant-badge');
    expect(screen.getByTestId('variant-badge')).toHaveAttribute('data-variant-key', 'C');
    expect(screen.getByLabelText('Variant C')).toHaveTextContent('C');

    const iconName = variantBadgeIconName('C');
    expect(iconName).toBe('variantBadgeC');
    expect(variantBadgeHeaderIcons[iconName]?.({
      fgColor: '#123456',
      bgColor: '#ffffff',
    })).toContain('>C</text>');
  });
});
