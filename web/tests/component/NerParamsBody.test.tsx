// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import * as api from '../../src/api/open';
import { NerParamsBody } from '../../src/components/action-panel/NerParamsBody';
import type { GeneratedActionParamsBodyProps } from '../../src/components/action-panel/GeneratedActionParamsBody';
import { sheetMeta } from '../support/actionFormFixtures';

vi.mock('../../src/components/PinnedArtifactDownload', () => ({
  PinnedArtifactDownload: ({ artifact, onInstalled }: {
    artifact: { ref: string }; onInstalled(): void;
  }) => <button onClick={onInstalled}>Install {artifact.ref}</button>,
}));
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

const props: GeneratedActionParamsBodyProps<'map.ner'> = {
  sheet: sheetMeta([], { id: '7' }),
  params: { source: ['story'], engine: 'spacy', labels: ['person'] },
  request: { scope: { kind: 'sheet_rows', sheet_id: 7 }, output_names: { entities: 'entities' } },
  setParams: () => {}, errors: {}, Field: () => null,
  engine: { id: 'spacy', label: 'spaCy', tier: 'local', available: false,
    downloadable_models: [{ ref: 'spacy:en_core_web_sm', display_name: 'spaCy English',
      revision: 'abc123', size: 48_000_000, license: 'MIT' }] },
};

it('offers the declared missing spaCy pack and refreshes catalog facts after installation', () => {
  const invalidate = vi.spyOn(api, 'invalidateActionCatalog').mockImplementation(() => {});
  render(<NerParamsBody {...props} />);
  fireEvent.click(screen.getByRole('button', { name: 'Install spacy:en_core_web_sm' }));
  expect(invalidate).toHaveBeenCalledOnce();
  expect(screen.queryByRole('button', { name: 'Install spacy:en_core_web_sm' })).not.toBeInTheDocument();
});

it('does not offer a spaCy download for an available engine or another selected engine', () => {
  const { rerender } = render(<NerParamsBody {...props} engine={{ ...props.engine!, available: true }} />);
  expect(screen.queryByRole('button', { name: /Install spacy/ })).not.toBeInTheDocument();
  rerender(<NerParamsBody {...props} params={{ ...props.params, engine: 'gliner' }} />);
  expect(screen.queryByRole('button', { name: /Install spacy/ })).not.toBeInTheDocument();
});
