import { createHash } from 'node:crypto';
import { appendFileSync, existsSync } from 'node:fs';
import path from 'node:path';
import type { Page } from '@playwright/test';

// Ordered: a tab or dropdown trigger should not also appear as a generic button.
const families = [
  ['Tabs and segmented controls', '[role="tab"], .segmented, .import-stage-stepper'],
  ['Dropdown triggers', 'select, [role="combobox"], button[aria-haspopup]'],
  ['Buttons and icon controls', 'button, [role="button"]'],
  ['Progress and loading', 'progress, [role="progressbar"], .panel-loading, .run-spinner, .activity-progress-count'],
  ['Warnings, errors and notices', '[role="alert"], .panel-error, .panel-empty, .settings-inline-status, .settings-validation-message, .activity-progress-card-detail, [class*="warning-text"], [class*="notice"], [class*="help-text"]'],
  ['Badges and chips', '.status-chip, .removable-chip, [class*="badge"], .engine-selector__chips > span'],
  ['Inputs, checkboxes and switches', 'input:not([type="hidden"]), textarea, [role="switch"], .toggle-row-switch'],
  ['Labels and help text', '.form-label, .form-hint, .form-help, .settings-field-description, .toggle-row-desc'],
] as const;

/** Crop real rendered controls without scrolling or reconstructing their markup. */
export async function captureElements(page: Page, output: string, context: string, scene: string, source: string) {
  const elements = await page.evaluate(groups => {
    const seen = new Set<Element>();
    return groups.flatMap(([family, selector]) => Array.from(document.querySelectorAll<HTMLElement>(selector)).flatMap(element => {
      if (seen.has(element) || element.closest('[hidden], [aria-hidden="true"]')) return [];
      const box = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      if (box.width < 4 || box.height < 4 || box.height > 180 || box.width > 1000
        || box.x < 0 || box.y < 0 || box.right > innerWidth || box.bottom > innerHeight
        || style.visibility !== 'visible' || style.opacity === '0') return [];
      // Exclude controls hidden by a modal, scroller clipping, or an overlapping panel.
      const exposed = [[0.2, 0.2], [0.8, 0.8], [0.5, 0.5]].every(([x, y]) => {
        const hit = document.elementFromPoint(box.x + box.width * x, box.y + box.height * y);
        return hit !== null && element.contains(hit);
      });
      if (!exposed) return [];
      seen.add(element);
      if (family === 'Tabs and segmented controls') element.querySelectorAll('button').forEach(child => seen.add(child));
      const classes = element.getAttribute('class') ?? '';
      const state = ['role', 'type', 'aria-selected', 'aria-pressed', 'aria-expanded', 'data-icon-size']
        .map(key => `${key}=${element.getAttribute(key) ?? ''}`).join(' ')
        + ` disabled=${element.matches(':disabled')} checked=${element.matches(':checked')}`;
      // Keep distinct implementations/states; collapse repeated labels using the same treatment.
      const appearance = [style.color, style.backgroundColor, style.fontSize, style.fontWeight,
        style.border, style.borderRadius, style.padding, style.boxShadow, style.height, style.lineHeight].join('|');
      const key = [family, element.tagName, classes, state, appearance].join('|');
      const name = element.getAttribute('aria-label') || element.innerText?.trim()
        || element.getAttribute('placeholder') || element.getAttribute('title') || element.tagName.toLowerCase();
      const x = Math.max(0, box.x - 3), y = Math.max(0, box.y - 3);
      return [{ family, key, name: name.slice(0, 140), selector: `${element.tagName.toLowerCase()}${classes ? `.${classes.trim().split(/\s+/).join('.')}` : ''}`,
        state, clip: { x, y, width: Math.min(innerWidth - x, box.width + 6), height: Math.min(innerHeight - y, box.height + 6) } }];
    }));
  }, families);
  for (const element of elements) {
    const file = `element-${createHash('sha256').update(element.key).digest('hex').slice(0, 16)}.png`;
    if (existsSync(path.join(output, file))) continue;
    await page.screenshot({ path: path.join(output, file), clip: element.clip, animations: 'disabled' });
    appendFileSync(path.join(output, 'captures.ndjson'), `${JSON.stringify({ kind: 'small',
      family: element.family, name: element.name, selector: element.selector, state: element.state,
      source, scene, context, file, theme: 'light' })}\n`);
  }
}
