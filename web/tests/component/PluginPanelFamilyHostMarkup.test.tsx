// @vitest-environment jsdom
import { cleanup, render } from '@testing-library/react';
import type { ComponentProps } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { SheetMeta } from '../../src/api/types';
import type { WorkbenchPanelDescriptor } from '../../src/workbench/descriptors';
import type { WorkbenchHostContext } from '../../src/workbench/hostContext';
import type { TrustedLocalPluginComponent } from '../../src/workbench/TrustedLocalPluginComponent';
import { PluginPanelHost } from '../../src/workbench/PluginPanelHost';
import { PluginDockTabHost } from '../../src/workbench/PluginDockTabHost';
import { PluginPeekHost } from '../../src/workbench/PluginPeekHost';
import { PluginDetailHost } from '../../src/workbench/PluginDetailHost';
import { SELECTION_SUMMARY_PANEL_COMPONENT_KEY } from '../../src/workbench/SelectionSummaryPanel';
import { sheetMeta } from '../support/actionFormFixtures';

// Rendered markup of the four panel-family hosts in every state. The data
// attributes, test ids and class names are read by Playwright specs and plugin
// CSS, so the snapshot must only change deliberately.

type RuntimeProps = ComponentProps<typeof TrustedLocalPluginComponent>;
vi.mock('../../src/workbench/TrustedLocalPluginComponent', () => ({
  TrustedLocalPluginComponent: (props: RuntimeProps) => <div data-testid="runtime-probe" data-component={props.componentKey} />,
}));

const sheet: SheetMeta = sheetMeta([{ id: '1', name: 'title', type: 'text' }, { id: '2', name: 'date', type: 'date' }, { id: '3', name: 'image', type: 'image' }], {
  id: '7',
  name: 'Documents',
  rowCount: 2,
});
const subject = { kind: 'row' as const, sheetId: sheet.id, rowId: '11' };

function hostContext(): WorkbenchHostContext {
  return {
    identity: { projectId: 'project', activeSheetId: sheet.id, routePath: '/p/project', activeRegion: null, activeContributionId: null },
    selection: { selectedRowIds: ['11'], activeRowId: '11', activeCell: null, activeColumnId: null,
      activeSourceId: null, activeEntityId: null, activeEvidenceLinkId: null },
    gridState: { filter: null, sort: null, viewId: null, columnOrder: [], frozenColumns: 0, dataVersion: 0 },
    gridFilter: { applySpec: vi.fn(), applyBbox: vi.fn(), applyValue: vi.fn(), applyEntity: vi.fn(), clear: vi.fn() },
    navigation: { openSheet: vi.fn(), openRow: vi.fn(), openColumn: vi.fn(), openSource: vi.fn(), openEvidence: vi.fn(),
      openMap: vi.fn(), openGraph: vi.fn(), openActionRoute: vi.fn(), openMainViewContribution: vi.fn() },
    actions: { runAction: async () => {}, previewAction: async () => {}, inspectProposal: vi.fn(),
      applyReviewDecision: async () => {}, pollSource: async () => {} },
    invalidation: { refreshSheets: async () => [], refreshHistory: async () => {}, refreshReviewCount: async () => {}, refreshGridRows: vi.fn() },
    policy: { confirmCost: async () => false, confirmEgress: async () => false, preferStaleEvidence: false },
    layoutProfile: { persistPersonalOverride: vi.fn(), persistProjectProfile: async () => {}, resetPersonalLayout: vi.fn(),
      resetProjectProfile: async () => {}, rewriteAlias: alias => alias },
  };
}

function descriptor(): WorkbenchPanelDescriptor {
  return {
    schemaVersion: 'frisket.workbench.panel.v1', id: 'plugin.test', kind: 'panel', ownerPluginId: 'plugin',
    title: 'Plugin test', componentKey: 'declared.Component', requires: [], dataRequirements: [],
    placements: [{ host: 'rightInspector', mode: 'panel' }, { host: 'bottomDock', mode: 'tab' },
      { host: 'modalOrPeek', mode: 'peek' }, { host: 'rowDetail', mode: 'section' }, { host: 'mainView', mode: 'pane' }],
    runtimeComponent: { pluginId: 'runtime-plugin', contributionId: 'runtime-contribution', componentKey: 'runtime.Component',
      moduleUrl: '/plugins/runtime.js', moduleKey: 'runtime', manifestSha256: 'manifest', packageSha256: 'package' },
  };
}

type Kind = 'panel' | 'dock' | 'peek' | 'detail';

function renderHost(kind: Kind, panel: WorkbenchPanelDescriptor, hostSheet: SheetMeta | null, context = hostContext()) {
  const common = { descriptor: panel, sheet: hostSheet, hostContext: context };
  if (kind === 'panel') return render(<PluginPanelHost {...common} />);
  if (kind === 'dock') return render(<PluginDockTabHost {...common} isActiveTab focusTab={() => {}} />);
  if (kind === 'peek') return render(<PluginPeekHost {...common} onDismiss={() => {}} />);
  return render(<PluginDetailHost {...common} host="rowDetail" subject={subject} />);
}

afterEach(() => { cleanup(); });

describe('panel-family host markup', () => {
  const kinds: Kind[] = ['panel', 'dock', 'peek', 'detail'];
  it.each(kinds)('%s: mounted', (kind) => {
    expect(renderHost(kind, descriptor(), sheet).container.innerHTML).toMatchSnapshot();
  });
  it.each(kinds)('%s: missing capability', (kind) => {
    const panel = { ...descriptor(), requires: [{ kind: 'hostCapability' as const, id: 'not.offered' }] };
    expect(renderHost(kind, panel, sheet).container.innerHTML).toMatchSnapshot();
  });
  it.each(kinds)('%s: unmet data requirement', (kind) => {
    const context = hostContext();
    context.selection.activeRowId = null;
    const panel = { ...descriptor(), dataRequirements: [{ kind: 'activeRow' as const }] };
    expect(renderHost(kind, panel, sheet, context).container.innerHTML).toMatchSnapshot();
  });
  it.each(kinds)('%s: no active sheet', (kind) => {
    expect(renderHost(kind, descriptor(), null).container.innerHTML).toMatchSnapshot();
  });
  it.each(kinds)('%s: missing component', (kind) => {
    expect(renderHost(kind, { ...descriptor(), runtimeComponent: undefined }, sheet).container.innerHTML).toMatchSnapshot();
  });
  it('panel: native component fallback', () => {
    const panel = { ...descriptor(), runtimeComponent: undefined, componentKey: SELECTION_SUMMARY_PANEL_COMPONENT_KEY };
    expect(renderHost('panel', panel, sheet).container.innerHTML).toMatchSnapshot();
  });
});
