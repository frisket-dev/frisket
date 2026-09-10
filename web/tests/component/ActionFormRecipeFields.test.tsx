// @vitest-environment jsdom
//
// Componentized from tests/e2e/recipe-forms.spec.ts's first test: each non-classify op (derive/
// reduce/judge) opens a form exposing
// its distinctive parameter. The spec's other three tests (backend-
// advertised bridge actions being reachable+runnable end-to-end, the
// engine-availability picker's real backend-computed copy, and an actual
// regex run updating sheet stats) need a live backend/dev-server and are
// left as e2e coverage — see the trimmed tests/e2e/recipe-forms.spec.ts.

import '@testing-library/jest-dom/vitest';
import { cleanup, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import { columnDef } from '../support/domainFixtures';
import { sheetMeta } from '../support/actionFormFixtures';
import { mockLocalProviders, mountTypedForm } from './cutoverUiF1TypedForm';

let dispose: (() => void) | undefined;
afterEach(() => { cleanup(); dispose?.(); dispose = undefined; vi.restoreAllMocks(); });

function rowsSheet() {
  return sheetMeta([
    columnDef({ id: '1', name: 'name', type: 'text' }),
    columnDef({ id: '2', name: 'note', type: 'text' }),
    columnDef({ id: '3', name: 'items', type: 'json' }),
  ], { id: '7' });
}

beforeEach(() => {
  mockLocalProviders();
});

const FORMS = [
  { kind: 'derive.table_from_list', field: 'derive-source-column-select' },
  { kind: 'reduce.group_summary', field: 'field-group_by' },
  { kind: 'map.judge', field: 'field-guidelines' },
];

for (const { kind, field } of FORMS) {
  it(`${kind} form exposes its '${field}' parameter`, () => {
    dispose = mountTypedForm({ kind, sheet: rowsSheet() }).dispose;
    expect(screen.getByTestId('generated-action-form')).toBeVisible();
    expect(screen.getByTestId(field)).toBeVisible();
    if (kind === 'derive.table_from_list') {
      expect(screen.getByTestId(field)).toHaveValue('3');
      expect(screen.getByTestId(field)).toHaveTextContent('items');
    }
  });
}
