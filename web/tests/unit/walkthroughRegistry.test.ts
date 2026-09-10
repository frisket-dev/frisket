import { describe, expect, it } from 'vitest';

import { walkthroughsForEdition } from '../../src/walkthrough/registry';
import {
  WALKTHROUGHS,
  type WalkthroughDefinition,
  type WalkthroughTarget,
} from '../../src/walkthrough/walkthroughs';
import { ACTION_PLACEMENTS } from '../../src/actions/model';
import { BASE_ACT_TAB_IDS } from '../../src/workbench/actSurface';
import {
  immutableEditionDescriptor,
  LOCAL_EDITION_DESCRIPTOR,
  TEAM_EDITION_DESCRIPTOR,
} from '../../src/editions/posture';

const CLOUD_GUIDE: WalkthroughDefinition = {
  id: 'open-weight-model-lab',
  title: 'Try an open-weight model',
  description: 'Synthetic downstream guide.',
  editions: ['cloud'],
  steps: [{
    id: 'open-sheet',
    title: 'Open the sheet',
    instruction: 'Open Small model lab.',
    target: { kind: 'sheet', id: 'Small model lab' },
    advance: 'click-target',
  }],
};

const edition = (id: string) => immutableEditionDescriptor({
  id,
  capabilities: {
    configurableNotificationDestinations: false,
    configurableNotificationEmail: false,
    identity: true,
    team: true,
  },
});

describe('edition walkthrough registry', () => {
  it('keeps the local guide in local and team, and uses a Cloud contribution', () => {
    expect(walkthroughsForEdition(LOCAL_EDITION_DESCRIPTOR).map((guide) => guide.id))
      .toContain('local-model-lab');
    expect(walkthroughsForEdition(TEAM_EDITION_DESCRIPTOR).map((guide) => guide.id))
      .toContain('local-model-lab');

    const cloud = walkthroughsForEdition(edition('cloud'), [CLOUD_GUIDE])
      .map((guide) => guide.id);
    expect(cloud).toContain('open-weight-model-lab');
    expect(cloud).not.toContain('local-model-lab');
    expect(cloud).toContain('regex-extract');
  });

  it('shows neither model-specific guide to operator posture', () => {
    const operator = walkthroughsForEdition(edition('operator'), [CLOUD_GUIDE]).map(
      (guide) => guide.id,
    );
    expect(operator).not.toContain('local-model-lab');
    expect(operator).not.toContain('open-weight-model-lab');
    expect(operator).toContain('regex-extract');
  });

  it('rejects a contribution that shadows an existing id even when hidden', () => {
    expect(() => walkthroughsForEdition(edition('operator'), [
      { ...CLOUD_GUIDE, id: 'local-model-lab' },
    ])).toThrow('Duplicate walkthrough id: local-model-lab');
  });
});

function directTarget(target: WalkthroughTarget): WalkthroughTarget {
  return target.kind === 'completed-run' ? directTarget(target.target) : target;
}

describe('walkthrough action targets', () => {
  it('names real tabs and canonical actions in the tab that displays them', () => {
    const tabIds = new Set<string>(BASE_ACT_TAB_IDS);
    for (const walkthrough of WALKTHROUGHS) {
      let activeTab: string | null = null;
      for (const step of walkthrough.steps) {
        const target = directTarget(step.target);
        if (target.kind === 'action-tab') {
          expect(tabIds.has(target.id), `${walkthrough.id}/${step.id} names an unknown tab`).toBe(true);
          activeTab = target.id;
        }
        if (target.kind === 'action') {
          const placement = ACTION_PLACEMENTS[target.id];
          expect(placement, `${walkthrough.id}/${step.id} names an unknown action`).toBeDefined();
          expect(activeTab, `${walkthrough.id}/${step.id} does not first reveal its action tab`)
            .toBe(placement?.tab);
        }
      }
    }
  });
});
