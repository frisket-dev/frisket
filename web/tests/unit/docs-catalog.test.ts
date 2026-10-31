import { expect, it } from 'vitest';
import { docsCatalog } from '../../scripts/export-docs-catalog';
import { servedActionCatalog } from '../support/servedActionCatalog';

it('exports visible action labels and placements through the actual frontend projection', () => {
  const catalog = docsCatalog(servedActionCatalog(), 'a'.repeat(40));
  const extract = catalog.actions.find((action) => action.id === 'map.extract');
  expect(extract).toMatchObject({ label: 'Extract fields', path: ['Analyze', 'EXTRACT'] });
  expect(extract?.inputSchema).toBeDefined();
  expect(extract?.settingsCoverage).toBe('audited');
  const basic = catalog.actions.find((action) => action.id === 'map.summarize');
  expect(basic).toMatchObject({ settingsCoverage: 'not-audited', params: [] });
  expect(catalog.actions.some((action) => action.id === 'media.read_pdf_text')).toBe(false);
  expect(new Set(catalog.actions.map((action) => action.id)).size).toBe(catalog.actions.length);
}, 30000);

it('exports the controls rendered by the Extract, OCR, and Transcribe forms', () => {
  const actions = docsCatalog(servedActionCatalog(), 'a'.repeat(40)).actions;
  const settings = (id: string) => actions.find((action) => action.id === id)!.params;
  expect(settings('map.extract')).toEqual(expect.arrayContaining([
    expect.objectContaining({ name: 'fields', label: 'Columns it creates' }),
    expect.objectContaining({ name: 'citation_mode', label: 'Citations' }),
    expect.objectContaining({ name: 'source_document_columns', label: 'Citation sources' }),
  ]));
  expect(settings('map.extract').map((param) => param.name)).not.toContain('grounding');
  expect(settings('map.extract').map((param) => param.name)).not.toContain('evidence_policy');
  expect(settings('media.ocr')).toEqual(expect.arrayContaining([
    expect.objectContaining({ name: 'source', label: 'Document' }),
    expect.objectContaining({ name: 'dpi', label: 'PDF render resolution (DPI)' }),
    expect.objectContaining({ name: 'searchable_pdf', label: 'Create searchable PDF' }),
  ]));
  expect(settings('media.transcribe')).toEqual(expect.arrayContaining([
    expect.objectContaining({ name: 'source', label: 'Media' }),
    expect.objectContaining({ name: 'diarize', label: 'Identify speakers' }),
    expect.objectContaining({ name: 'model_size', label: 'Model size' }),
    expect.objectContaining({ name: 'vad', label: 'Voice Activity Detection' }),
  ]));
}, 30000);

it('rejects an invalid catalog revision', () => {
  expect(() => docsCatalog(servedActionCatalog(), 'not-a-sha')).toThrow('full lowercase Git SHA');
});
