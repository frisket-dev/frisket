import type { Page } from '@playwright/test';
import type { HttpColumnRunsPage } from '../../src/generated/openHttpContracts';

/** These picker tests seed extracted values directly. Simulate only the
 * producer-history response; materialization and export still use the server. */
export async function stubPdfTableProducer(page: Page, pid: string, columnId: number) {
  const run: NonNullable<HttpColumnRunsPage['current_run']> = {
    run_id: 4100, action_kind: 'media.extract_pdf_tables', action_name: 'Extract PDF tables',
    status: 'completed', current: true, spec: {}, total_rows: 1, completed_rows: 1,
    failed_rows: 0, cost_actual: 0, started_at: '2026-01-01T00:00:00Z', finished_at: null,
    duration_ms: 1, model: null, tokens_in: null, tokens_out: null,
  };
  const history: HttpColumnRunsPage = {
    column: { id: columnId, name: 'pdf_tables', type: 'json', ai_generated: true,
      current_run_id: 4100, latest_run_id: 4100, mixed_origins: false },
    current_run: run, latest_run: run, current_run_loaded: true, latest_run_loaded: true,
    offset: 0, limit: 1, total: 1, has_more: false, next_offset: null, runs: [run],
  };
  await page.route(`**/api/projects/${pid}/columns/${columnId}/runs*`, (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify(history),
  }));
}
