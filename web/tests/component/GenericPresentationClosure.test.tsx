// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach, beforeAll, describe, expect, it } from 'vitest';

import { actionTemplatesFromCatalog } from '../../src/actions/model';
import {
  deriveActionPresentationCatalog,
  isClosedVocabularyControl,
} from '../../src/components/action-panel/actionPresentation';
import { completeMappedActionCatalog } from '../support/actionCatalogFixtures';

afterEach(cleanup);

describe('generic action-presentation closure', () => {
  let payload: ReturnType<typeof completeMappedActionCatalog>;
  beforeAll(() => {
    payload = completeMappedActionCatalog();
  }, 30_000);

  it('accepts backend-owned maxLength metadata only on generic text controls', () => {
    expect(isClosedVocabularyControl({
      name: 'context',
      label: 'Context',
      input: 'text',
      maxLength: 500,
    })).toBe(true);
    expect(isClosedVocabularyControl({
      name: 'vad',
      label: 'VAD',
      input: 'checkbox',
      maxLength: 500,
    })).toBe(false);
  });

  it('renders every mapped core action through the generated or generic form', () => {
    // No kind owns a bespoke drawer or section any more: the presentation
    // partition is a pure consequence of the catalog/launcher join.
    const dispositions = deriveActionPresentationCatalog(
      payload,
      actionTemplatesFromCatalog(payload),
    );
    const mappedKinds = payload.actions.map((entry) => entry.kind);
    expect([...dispositions.keys()]).toEqual(mappedKinds);
    for (const kind of mappedKinds) {
      expect(['hidden', 'generic', 'generated'], kind).toContain(dispositions.get(kind)?.kind);
    }
    expect(mappedKinds).not.toContain('research.project_answer');
  });
});
