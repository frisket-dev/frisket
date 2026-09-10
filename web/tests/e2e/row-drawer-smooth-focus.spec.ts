// RED-FIRST: selected-cell focus should use a scoped drawer-body smooth scroll,
// with reduced motion falling back to an instant local scroll.

import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import {
  createProject,
  importCsv,
  openCellDrawer,
  openProject,
  sheetColumns,
  type WireColumn,
  uniqueName,
} from './helpers';

type ScrollCall = { top: number | null; behavior: string | null };
type ScrollProbeWindow = Window & {
  __rowDrawerScrollCalls?: ScrollCall[];
  __rowFieldScrollIntoViewCalls?: string[];
};

const FIELD_COUNT = 20;
const TARGET_FIELD = 'field_16';

async function installScrollProbe(page: Page) {
  await page.addInitScript(() => {
    const probeWindow = window as ScrollProbeWindow;
    probeWindow.__rowDrawerScrollCalls = [];
    probeWindow.__rowFieldScrollIntoViewCalls = [];

    const originalScrollTo = HTMLElement.prototype.scrollTo;
    HTMLElement.prototype.scrollTo = function rowDrawerScrollTo(...args: unknown[]) {
      if (this.classList.contains('drawer-body')) {
        const first = args[0];
        const top =
          typeof first === 'number'
            ? first
            : first && typeof first === 'object' && 'top' in first
              ? Number((first as ScrollToOptions).top)
              : null;
        const behavior =
          first && typeof first === 'object' && 'behavior' in first
            ? String((first as ScrollToOptions).behavior)
            : null;
        probeWindow.__rowDrawerScrollCalls?.push({
          top: Number.isFinite(top) ? top : null,
          behavior,
        });
        if (Number.isFinite(top)) this.scrollTop = Number(top);
        return;
      }
      return Reflect.apply(originalScrollTo, this, args);
    } as typeof HTMLElement.prototype.scrollTo;

    const originalScrollIntoView = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = function rowFieldScrollIntoView(...args: unknown[]) {
      const fieldId = (this as HTMLElement).dataset?.rowFieldId;
      if (fieldId) probeWindow.__rowFieldScrollIntoViewCalls?.push(fieldId);
      return Reflect.apply(originalScrollIntoView, this, args);
    } as typeof Element.prototype.scrollIntoView;
  });
}

async function seedWideSheet(request: APIRequestContext, prefix: string) {
  const pid = await createProject(request, uniqueName(prefix));
  const headers = Array.from({ length: FIELD_COUNT }, (_, i) => `field_${String(i).padStart(2, '0')}`);
  const values = headers.map((name) => `"${name} value with enough text to give the drawer row some height"`);
  const sheetId = await importCsv(request, pid, 'wide-row.csv', `${headers.join(',')}\n${values.join(',')}\n`);
  const columns = await sheetColumns(request, pid, sheetId);
  return { pid, sheetId, columns };
}

async function selectTargetField(page: Page, columns: WireColumn[]) {
  await expect(page.getByTestId('row-drawer')).toHaveCount(0);
  // 'field_00' is a plain non-AI text column (now in-place editable —
  // grid-in-place-edit-v1), so the floating icon, not Enter, opens the drawer.
  await openCellDrawer(page, columns, 'field_00', 0);
  await expect(page.getByTestId('row-field-field_00')).toHaveAttribute('data-selected-cell', 'true');
  for (let i = 0; i < 16; i += 1) {
    await page.keyboard.press('ArrowRight');
  }
  const target = page.getByTestId(`row-field-${TARGET_FIELD}`);
  await expect(target).toHaveAttribute('data-selected-cell', 'true');
  await expect(target).toBeInViewport();
}

test('row drawer focuses selected fields with scoped smooth scrolling', async ({ page, request }) => {
  await installScrollProbe(page);
  const { pid, sheetId, columns } = await seedWideSheet(request, 'e2e-row-smooth-focus');
  await openProject(page, pid, sheetId);

  await selectTargetField(page, columns);

  const calls = await page.evaluate(
    () => (window as ScrollProbeWindow).__rowDrawerScrollCalls ?? [],
  );
  expect(calls.some((call) => call.behavior === 'smooth' && (call.top ?? 0) > 0)).toBeTruthy();
  expect(
    await page.evaluate(() => (window as ScrollProbeWindow).__rowFieldScrollIntoViewCalls ?? []),
  ).toEqual([]);
});

test('row drawer selected-field scroll respects reduced motion', async ({ page, request }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await installScrollProbe(page);
  const { pid, sheetId, columns } = await seedWideSheet(request, 'e2e-row-reduced-focus');
  await openProject(page, pid, sheetId);

  await selectTargetField(page, columns);

  const calls = await page.evaluate(
    () => (window as ScrollProbeWindow).__rowDrawerScrollCalls ?? [],
  );
  expect(calls.some((call) => call.behavior === 'auto' && (call.top ?? 0) > 0)).toBeTruthy();
  expect(calls.every((call) => call.behavior !== 'smooth')).toBeTruthy();
});
