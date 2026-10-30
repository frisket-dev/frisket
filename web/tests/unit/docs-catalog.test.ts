import { expect, it } from 'vitest';
import { docsCatalog } from '../../scripts/export-docs-catalog';
import { servedActionCatalog } from '../support/servedActionCatalog';

it('exports visible action labels and placements through the actual frontend projection', () => {
  const catalog = docsCatalog(servedActionCatalog());
  const extract = catalog.actions.find((action) => action.id === 'map.extract');
  expect(extract).toMatchObject({ label: 'Extract fields', path: ['Analyze', 'EXTRACT'] });
  expect(extract?.inputSchema).toBeDefined();
  expect(catalog.actions.some((action) => action.id === 'media.read_pdf_text')).toBe(false);
  expect(new Set(catalog.actions.map((action) => action.id)).size).toBe(catalog.actions.length);
}, 30000);
