// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ExtractPageOverlay } from '../../src/workbench/extract/ExtractPageOverlay';
import { ExtractFields } from '../../src/workbench/extract/ExtractFields';
import { ExtractPreview } from '../../src/workbench/extract/ExtractPreview';
import { assignUnclaimedFields, changeRegion, normalizeBox, previewOutcome, regionInsideSpan, removeAnnotation, spanOnPage, templateDefaults, templateIssue, textInRegion, type ExtractionTemplate } from '../../src/workbench/extract/types';
vi.mock('../../src/media/pdfjsSetup', () => ({ pdfjsLib: { getDocument: vi.fn(), TextLayer: class {} } }));
import { DocumentReader } from '../../src/workbench/DocumentReader';
import { pdfjsLib } from '../../src/media/pdfjsSetup';

const template: ExtractionTemplate = {
  reference_blob_id: 'example', reference_page: null, reference_fingerprint: 'fingerprint', fields: [
    { id: 'name', name: 'Name', key: { page: 1, box: { x0: .1, x1: .2, y0: .1, y1: .15 } }, value: { page: 1, box: { x0: .3, x1: .6, y0: .1, y1: .15 } }, section_id: null },
  ], sections: [], ignore_bands: [], expand_values: false, look_every_page: true, continue_across_pages: false,
};
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe('visual extraction geometry', () => {
  it('retains the selected reference page when hydrating a template', () => {
    expect(templateDefaults({ ...template, reference_page: 2 }).reference_page).toBe(2);
  });
  it('does not steal assigned fields, and requires both boxes inside the first record', () => {
    const section = { id: 'new', name: 'New records', first: { start: { page: 1, y: .05 }, end: { page: 1, y: .2 } },
      rest: { start: { page: 1, y: .2 }, end: { page: 1, y: .9 } } };
    const assigned = { ...template.fields[0], section_id: 'old' };
    const outside = { ...template.fields[0], id: 'outside', value: { page: 2, box: template.fields[0].value.box } };
    const result = assignUnclaimedFields([assigned, outside, template.fields[0]], section);
    expect(result.map((field) => field.section_id)).toEqual(['old', null, 'new']);
    expect(regionInsideSpan(outside.value, section.first)).toBe(false);
    expect(templateIssue({ ...template, sections: [section] }, section.id)).toContain('add a key/value pair inside record 1');
    const valid = { ...template, sections: [section], fields: [result[2]] };
    expect(templateIssue(valid, section.id)).toBeNull();
    expect(templateIssue({ ...valid, sections: [{ ...section, first: { ...section.first, end: { page: 1, y: .11 } } }] }, section.id)).toContain('Enlarge the band or move the boxes');
  });
  it('normalizes/clamps backward gestures and full-width bands', () => {
    expect(normalizeBox({ x: 1.2, y: .8 }, { x: -.1, y: .2 })).toEqual({ x0: 0, y0: .2, x1: 1, y1: .8 });
    expect(normalizeBox({ x: .4, y: .2 }, { x: .5, y: .6 }, true)).toEqual({ x0: 0, x1: 1, y0: .2, y1: .6 });
  });
  it('preserves the other page when adjusting a spanning section edge', () => {
    const value = { ...template, sections: [{ id: 'r', name: 'Records', first: { start: { page: 1, y: .1 }, end: { page: 1, y: .3 } },
      rest: { start: { page: 1, y: .3 }, end: { page: 3, y: .7 } } }] };
    const updated = changeRegion(value, { id: 'r', kind: 'rest' }, { page: 3, box: { x0: 0, x1: 1, y0: 0, y1: .8 } });
    expect(updated.sections[0].rest).toEqual({ start: { page: 1, y: .3 }, end: { page: 3, y: .8 } });
    expect(spanOnPage(updated.sections[0].rest, 2)).toEqual({ x0: 0, y0: 0, x1: 1, y1: 1 });
    expect(spanOnPage(updated.sections[0].rest, 4)).toBeNull();
  });
  it('deletes a key/value pair together and dependent section fields together', () => {
    expect(removeAnnotation(template, { kind: 'key', id: 'name' }).fields).toEqual([]);
    const repeated = { ...template, fields: [{ ...template.fields[0], section_id: 'r' }] };
    expect(removeAnnotation(repeated, { kind: 'rest', id: 'r' }).fields).toEqual([]);
  });
  it('reads only tokens whose center falls inside a value region; blank remains blank', () => {
    const document = { source_fingerprint: 'f', pages: [{ page: 1, width: 100, height: 100, tokens: [
      { text: 'NAME', box: template.fields[0].key.box, granularity: 'word' as const },
    ] }] };
    expect(textInRegion(document, template.fields[0].key)).toBe('NAME');
    expect(textInRegion(document, template.fields[0].value)).toBe('');
  });
});

describe('annotation interactions', () => {
  it('uses server page images and dimensions rather than PDF.js CropBox geometry in extraction mode', async () => {
    vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(832);
    vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(900);
    const pages = [{ page: 1, width: 800, height: 1000, url: '/blobs/cropped/pages/1/image' }, { page: 2, width: 800, height: 1000, url: '/blobs/cropped/pages/2/image' }];
    render(<DocumentReader media={{ url: '/blobs/cropped', label: 'cropped.pdf' }} mediaKind="pdf" title="cropped.pdf"
      layout="single" fit="width" videoFit="full" onVideoFitChange={() => undefined} textLayer={false} onPageCount={() => undefined}
      rowKey="1" onOpenDetail={() => undefined} canOpenDetail={false} optionsOpen={false} onToggleOptions={() => undefined}
      optionsPopover={null} selectionCount={0} pageImages={pages} renderPageOverlay={(page) => <div data-testid="annotation-page">{page}</div>} />);
    expect(await screen.findByRole('img', { name: 'Page 1' })).toHaveAttribute('src', pages[0].url);
    expect(screen.getByRole('img', { name: 'Page 1' })).toHaveStyle({ width: '800px', height: '1000px' });
    expect(pdfjsLib.getDocument).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId('document-page-next'));
    expect(await screen.findByRole('img', { name: 'Page 2' })).toHaveAttribute('src', pages[1].url);
    expect(screen.getByTestId('annotation-page')).toHaveTextContent('2');
  });
  it('offers keyboard selection, bounded movement and deletion for the selected region', () => {
    const onChange = vi.fn(); const onDelete = vi.fn(); const onSelect = vi.fn();
    render(<ExtractPageOverlay page={1} template={template} tool="select" selected={{ kind: 'value', id: 'name' }} onSelect={onSelect} onDraw={vi.fn()} onChange={onChange} onDelete={onDelete} />);
    const value = screen.getByRole('button', { name: 'Name value' });
    fireEvent.click(value);
    expect(onSelect).toHaveBeenCalledWith({ kind: 'value', id: 'name' });
    fireEvent.keyDown(value, { key: 'ArrowRight' });
    expect(onChange.mock.calls[0][1].box.x0).toBeCloseTo(.302);
    fireEvent.keyDown(value, { key: 'Backspace' });
    expect(onDelete).toHaveBeenCalledWith({ kind: 'value', id: 'name' });
  });
  it('muted other-document overlays cannot edit the reference template', () => {
    const onChange = vi.fn(); const onDelete = vi.fn();
    render(<ExtractPageOverlay page={1} template={template} tool="select" selected={null} muted onSelect={vi.fn()} onDraw={vi.fn()} onChange={onChange} onDelete={onDelete} />);
    const value = screen.getByRole('button', { name: 'Name value' });
    expect(value).toBeDisabled();
    fireEvent.keyDown(value, { key: 'Delete' });
    expect(onDelete).not.toHaveBeenCalled();
  });
  it('editing a field name does not delete its box when Backspace is pressed', () => {
    const onDelete = vi.fn(); const onChange = vi.fn();
    render(<ExtractFields template={template} reference={null} selected={null} preview={null} onChange={onChange} onSelect={vi.fn()} onDelete={onDelete} />);
    const input = screen.getByLabelText('Column name for Name');
    fireEvent.keyDown(input, { key: 'Backspace' });
    expect(onDelete).not.toHaveBeenCalled();
    fireEvent.change(input, { target: { value: 'Person' } });
    expect(onChange.mock.calls[0][0].fields[0].name).toBe('Person');
    expect(screen.getByText('empty in this example')).toBeInTheDocument();
  });
  it('lets the first reference record span pages without crossing the remaining-record boundary', () => {
    const onChange = vi.fn();
    const repeated = { ...template, sections: [{ id: 'r', name: 'Records', first: { start: { page: 1, y: .1 }, end: { page: 1, y: .8 } },
      rest: { start: { page: 2, y: .4 }, end: { page: 3, y: .8 } } }] };
    render(<ExtractFields template={repeated} reference={null} selected={null} preview={null} onChange={onChange} onSelect={vi.fn()} onDelete={vi.fn()} />);
    fireEvent.change(screen.getByLabelText('First record ends on page'), { target: { value: '2' } });
    expect(onChange.mock.calls[0][0].sections[0].first.end).toEqual({ page: 2, y: .4 });
    fireEvent.change(screen.getByLabelText('First record ends on page'), { target: { value: '3' } });
    expect(onChange).toHaveBeenCalledTimes(1);
  });
  it('preserves empty, not found and zero-record outcomes distinctly and navigates citations', () => {
    const onSelect = vi.fn();
    const empty = { text: '', status: 'empty' as const, regions: [template.fields[0].value], diagnostic: null };
    const documents = [
      { row_id: 1, blob_id: 'a', filename: 'a.pdf', result: { records: [{ cells: { name: empty } }], diagnostics: [], outcome: 'extracted' as const } },
      { row_id: 2, blob_id: 'b', filename: 'b.pdf', result: { records: [{ cells: { name: { text: null, status: 'not_found' as const, regions: [], diagnostic: 'Missing label' } } }], diagnostics: [], outcome: 'extracted' as const } },
      { row_id: 3, blob_id: 'c', filename: 'c.pdf', result: { records: [], diagnostics: [], outcome: 'zero_records' as const } },
    ];
    const { rerender } = render(<ExtractPreview template={template} preview={{ documents, truncated: false }} onSelect={onSelect} />);
    fireEvent.click(screen.getByRole('button', { name: 'empty' }));
    expect(onSelect).toHaveBeenCalledWith(documents[0], empty);
    expect(screen.getByText('⚠ not found')).toBeInTheDocument();
    expect(screen.getByText('No repeated records found')).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('Preview sample: 3 documents.');
    expect(previewOutcome(documents[1].result)).toEqual({ warning: true, text: 'Missing label' });
    expect(previewOutcome(documents[0].result)).toEqual({ warning: false, text: '1 records' });
    expect(previewOutcome(documents[2].result)).toEqual({ warning: false, text: 'No repeated records found' });
    rerender(<ExtractPreview template={template} preview={{ documents, truncated: true }} onSelect={onSelect} />);
    expect(screen.getByRole('status')).toHaveTextContent('Not all selected documents or rows are shown.');
    const warned = { ...documents[0], result: { ...documents[0].result, records: [{ cells: {
      name: { ...empty, text: 'Alice', status: 'extracted' as const, diagnostic: 'A word crosses the selected boundary' },
    } }] } };
    rerender(<ExtractPreview template={template} preview={{ documents: [warned], truncated: false }} onSelect={onSelect} />);
    expect(previewOutcome(warned.result)).toEqual({ warning: true, text: 'A word crosses the selected boundary' });
    expect(screen.getByLabelText('A word crosses the selected boundary')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Alice/ })).toHaveTextContent('Alice');
  });
});
