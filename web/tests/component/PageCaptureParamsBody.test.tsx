// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import { PageCaptureParamsBody } from '../../src/components/action-panel/PageCaptureParamsBody';
import { sheetMeta } from '../support/actionFormFixtures';

afterEach(cleanup);

function renderBody(errors: Parameters<typeof PageCaptureParamsBody>[0]['errors']) {
  return render(<PageCaptureParamsBody sheet={sheetMeta([])}
    request={{ scope: { kind: 'sheet_rows', sheet_id: 1 }, sheet_name: 'S', output_names: {} }}
    params={{ source: 'url' }} setParams={vi.fn()} errors={errors}
    Field={({ name }) => <div data-testid={`field-${name}`} />} />);
}

it('exposes size and timeout validation errors with the alert role', () => {
  renderBody({
    max_bytes: { ok: false, message: 'Size is too large.' },
    timeout_ms: { ok: false, message: 'Timeout is too long.' },
  });
  const alerts = screen.getAllByRole('alert');
  expect(alerts.map((alert) => alert.textContent)).toEqual(['Size is too large.', 'Timeout is too long.']);
});
