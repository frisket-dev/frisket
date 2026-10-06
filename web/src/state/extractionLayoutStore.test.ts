import { expect, it } from 'vitest';
import type { SavedExtractionTemplate } from '../api/documentExtraction';
import { createExtractionLayoutStore } from './extractionLayoutStore';

const layout = (expandValues = false): SavedExtractionTemplate => ({
  id: 1,
  name: 'Layout 1',
  sheet_id: 1,
  source: 'Document',
  source_column_id: 1,
  reference_row_id: null,
  repeat_group_id: null,
  has_applied: false,
  draft: {
    reference_blob_id: '',
    reference_page: null,
    reference_fingerprint: '',
    fields: [],
    sections: [],
    ignore_bands: [],
    expand_values: expandValues,
    look_every_page: true,
    continue_across_pages: false,
    pending: null,
  },
});

it('removes an unmounted context once it has no draft or pending write', () => {
  const store = createExtractionLayoutStore('project');
  const first = store.getContext('1', 'Document');
  first.retain();
  first.restore(layout());
  first.release();

  expect(store.getContext('1', 'Document')).not.toBe(first);
});

it('keeps a failed draft available to the next same-project mount', async () => {
  const store = createExtractionLayoutStore('project');
  const first = store.getContext('1', 'Document');
  first.retain();
  first.restore(layout());
  first.cacheDraft(layout(true));
  await expect(first.save(layout(true), async () => {
    throw new Error('Disk full');
  })).rejects.toThrow('Disk full');
  first.release();

  const second = store.getContext('1', 'Document');
  expect(second).toBe(first);
  expect(second.restore(layout()).draft.expand_values).toBe(true);
});

it('reattaches the same context across a dispose and effect replay while its flush settles', async () => {
  const store = createExtractionLayoutStore('project');
  const context = store.getContext('1', 'Document');
  context.retain();
  context.restore(layout());
  context.cacheDraft(layout(true));

  store.dispose();
  const flushed = context.save(layout(true), async () => layout(true));
  context.release();
  context.retain();

  await flushed;
  expect(store.getContext('1', 'Document')).toBe(context);
  expect(context.savedKey(1)).toBeDefined();
});
