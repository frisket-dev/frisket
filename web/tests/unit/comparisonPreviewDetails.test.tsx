// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import type { PreviewSampleResult } from '../../src/api/types';
import { ComparisonPreviewDetails } from '../../src/workbench/MediaCompareShell';

afterEach(cleanup);
it('retains downloadable preview files, warnings and measured usage', () => {
  const url = '/api/projects/test/actions/v1/preview/sample/artifacts/pdf';
  const preview = { kind: 'table', columns: [{ name: 'pdf', columnType: 'file' }],
    rows: [{ pdf: { value: url } }], warnings: ['One page has no text geometry.'],
    accounting: { cost_actual: 0.012, model_call_count: 2 },
  } as unknown as PreviewSampleResult;
  render(<ComparisonPreviewDetails preview={preview} elapsedMs={1200} />);
  expect(screen.getByRole('link', { name: 'Download pdf' })).toHaveAttribute('href', url);
  expect(screen.getByText('One page has no text geometry.')).toBeInTheDocument();
  expect(screen.getByText(/Provider cost:/)).toHaveTextContent('2 model calls');
  expect(screen.getByText(/Elapsed:/)).toBeInTheDocument();
});

it('does not turn arbitrary output strings into download links', () => {
  const preview = { kind: 'table', columns: [{ name: 'pdf', columnType: 'file' }],
    rows: [{ pdf: { value: 'javascript:alert(1)' } }], warnings: [],
  } as unknown as PreviewSampleResult;
  render(<ComparisonPreviewDetails preview={preview} />);
  expect(screen.queryByRole('link')).not.toBeInTheDocument();
});
