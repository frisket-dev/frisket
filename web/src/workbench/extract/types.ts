import type { HttpExtractionDocumentResponse, HttpExtractionPreviewRequest } from '../../generated/openHttpContracts';
import type { ExtractionPreviewDocument } from '../../api/documentExtraction';

export function previewOutcome(result: ExtractionPreviewDocument['result']): { warning: boolean; text: string } {
  const missing = result.records.flatMap((record) => Object.values(record.cells)).find((cell) => cell.status === 'not_found');
  return { warning: result.outcome === 'alignment_failed' || result.diagnostics.length > 0 || Boolean(missing),
    text: result.diagnostics[0] ?? missing?.diagnostic ?? (missing ? 'Some fields were not found'
      : result.outcome === 'alignment_failed' ? 'Could not align this document'
      : result.outcome === 'zero_records' ? 'No repeated records found' : `${result.records.length} records`) };
}

type WireTemplate = HttpExtractionPreviewRequest['template'];
export type ExtractionField = Required<WireTemplate['fields'][number]>;
export type PageRegion = ExtractionField['key'];
export type Box = PageRegion['box'];
export type RepeatedSection = Required<NonNullable<WireTemplate['sections']>[number]>;
export type PageSpan = RepeatedSection['first'];
export type ExtractionTemplate = Omit<Required<WireTemplate>, 'fields' | 'sections'> & { fields: ExtractionField[]; sections: RepeatedSection[] };
export type PositionedDocument = HttpExtractionDocumentResponse['document'];

export function templateDefaults(template: WireTemplate): ExtractionTemplate {
  return { ...template, expand_values: template.expand_values ?? false, look_every_page: template.look_every_page ?? true,
    continue_across_pages: template.continue_across_pages ?? false, ignore_bands: template.ignore_bands ?? [],
    sections: (template.sections ?? []).map((section) => ({ ...section, name: section.name ?? 'Repeated section' })),
    fields: template.fields.map((field) => ({ ...field, section_id: field.section_id ?? null })) };
}
export type ExtractTool = 'select' | 'key' | 'repeat' | 'ignore';
export type AnnotationTarget = { kind: 'key' | 'value'; id: string } | { kind: 'first' | 'rest'; id: string } | { kind: 'ignore'; id: string };

export function textInRegion(document: PositionedDocument | null, region: PageRegion): string {
  return document?.pages.find((page) => page.page === region.page)?.tokens.filter(({ box }) => {
    const x = (box.x0 + box.x1) / 2;
    const y = (box.y0 + box.y1) / 2;
    return x >= region.box.x0 && x <= region.box.x1 && y >= region.box.y0 && y <= region.box.y1;
  }).map(({ text }) => text).join(' ') ?? '';
}

export function normalizeBox(a: { x: number; y: number }, b: { x: number; y: number }, band = false): Box {
  const clamp = (n: number) => Math.max(0, Math.min(1, n));
  return { x0: band ? 0 : clamp(Math.min(a.x, b.x)), y0: clamp(Math.min(a.y, b.y)),
    x1: band ? 1 : clamp(Math.max(a.x, b.x)), y1: clamp(Math.max(a.y, b.y)) };
}

export function changeRegion(template: ExtractionTemplate, target: AnnotationTarget, region: PageRegion): ExtractionTemplate {
  if (target.kind === 'ignore') return { ...template, ignore_bands: template.ignore_bands.map((band, i) => String(i) === target.id ? { box: region.box } : band) };
  if (target.kind === 'key' || target.kind === 'value') return { ...template, fields: template.fields.map((field) => field.id === target.id ? { ...field, [target.kind]: region } : field) };
  return { ...template, sections: template.sections.map((section) => {
    if (section.id !== target.id) return section;
    const span = section[target.kind as 'first' | 'rest'];
    const next = { start: span.start.page === region.page ? { page: region.page, y: region.box.y0 } : span.start,
      end: span.end.page === region.page ? { page: region.page, y: region.box.y1 } : span.end };
    if (next.start.page > next.end.page || (next.start.page === next.end.page && next.start.y >= next.end.y)) return section;
    const updated = { ...section, [target.kind]: next };
    if (updated.first.end.page > updated.rest.start.page || (updated.first.end.page === updated.rest.start.page && updated.first.end.y > updated.rest.start.y)) return section;
    return updated;
  }) };
}

export function spanFromRegion(region: PageRegion): PageSpan {
  return { start: { page: region.page, y: region.box.y0 }, end: { page: region.page, y: region.box.y1 } };
}

export function spanOnPage(span: PageSpan, page: number): Box | null {
  if (page < span.start.page || page > span.end.page) return null;
  const box = { x0: 0, x1: 1, y0: page === span.start.page ? span.start.y : 0, y1: page === span.end.page ? span.end.y : 1 };
  return box.y0 < box.y1 ? box : null;
}

export function removeAnnotation(template: ExtractionTemplate, target: AnnotationTarget): ExtractionTemplate {
  if (target.kind === 'ignore') return { ...template, ignore_bands: template.ignore_bands.filter((_, i) => String(i) !== target.id) };
  if (target.kind === 'key' || target.kind === 'value') return { ...template, fields: template.fields.filter((field) => field.id !== target.id) };
  return { ...template, sections: template.sections.filter((section) => section.id !== target.id),
    fields: template.fields.filter((field) => field.section_id !== target.id) };
}
