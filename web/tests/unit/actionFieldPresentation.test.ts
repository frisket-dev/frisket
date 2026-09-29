import { expect, it } from 'vitest';

import { generatedActionTemplateFromCatalogEntry } from '../../src/actions/model';
import { syntheticActionCatalogEntry } from '../support/actionCatalogFixtures';

it('projects the schema textarea presentation without changing ordinary strings', () => {
  const entry = syntheticActionCatalogEntry('map.field_presentation', {
    input_schema: {
      type: 'object',
      properties: {
        context: {
          type: 'string',
          title: 'Dataset context',
          description: 'Background for the model.',
          'x-frisket-input': 'textarea',
        },
        reference: {
          type: 'string',
          title: 'Reference',
          description: 'A short lookup value.',
        },
      },
    },
    ui_hints: {
      form: 'generated',
      category: 'text',
      semantic_controls: {},
      source_requirements: [],
      logical_outputs: [],
    },
  });

  const template = generatedActionTemplateFromCatalogEntry(entry);

  expect(template?.params).toEqual([
    expect.objectContaining({
      name: 'context',
      label: 'Dataset context',
      hint: 'Background for the model.',
      input: 'textarea',
    }),
    expect.objectContaining({
      name: 'reference',
      label: 'Reference',
      hint: 'A short lookup value.',
      input: 'text',
    }),
  ]);
});
