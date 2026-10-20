// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import type { ComponentProps } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { SheetMeta } from '../../src/api/types';
import type { WorkbenchPanelDescriptor, WorkbenchViewDescriptor } from '../../src/workbench/descriptors';
import { IMAGE_GALLERY_VIEW_DESCRIPTOR } from '../../src/workbench/descriptors';
import type { WorkbenchHostContext } from '../../src/workbench/hostContext';
import type { PluginProjectionViewApi, PluginProjectionViewContext } from '../../src/workbench/pluginProjectionViewContext';
import type { PluginViewContext } from '../../src/workbench/pluginViewContext';
import type { TrustedLocalPluginComponent } from '../../src/workbench/TrustedLocalPluginComponent';
import { PluginPanelHost } from '../../src/workbench/PluginPanelHost';
import { PluginDockTabHost } from '../../src/workbench/PluginDockTabHost';
import { PluginPeekHost } from '../../src/workbench/PluginPeekHost';
import { PluginDetailHost } from '../../src/workbench/PluginDetailHost';
import { PluginMainViewHost } from '../../src/workbench/PluginMainViewHost';
import { SELECTION_SUMMARY_PANEL_COMPONENT_KEY } from '../../src/workbench/SelectionSummaryPanel';

type RuntimeProps = ComponentProps<typeof TrustedLocalPluginComponent>;
const runtimeMount = vi.hoisted(() => vi.fn<(props: RuntimeProps) => void>());
const galleryMount = vi.hoisted(() => vi.fn<(ctx: PluginViewContext) => void>());
const timelineMount = vi.hoisted(() => vi.fn<(ctx: PluginProjectionViewContext) => void>());
vi.mock('../../src/workbench/TrustedLocalPluginComponent', () => ({
  TrustedLocalPluginComponent: (props: RuntimeProps) => {
    runtimeMount(props);
    return <div data-testid="runtime-probe" />;
  },
}));
vi.mock('../../src/workbench/ImageGallery', () => ({
  ImageGallery: ({ ctx }: { ctx: PluginViewContext }) => {
    galleryMount(ctx);
    return <div data-testid="native-gallery" />;
  },
}));
vi.mock('../../src/workbench/TimelineProjectionView', () => ({
  TimelineProjectionView: ({ ctx }: { ctx: PluginProjectionViewContext }) => {
    timelineMount(ctx);
    return <div data-testid="native-timeline" />;
  },
}));

const sheet: SheetMeta = { id: '7', name: 'Documents', rowCount: 2,
  columns: [{ id: '1', name: 'title', type: 'text' }, { id: '2', name: 'date', type: 'date' }, { id: '3', name: 'image', type: 'image' }] };
const subject = { kind: 'row' as const, sheetId: sheet.id, rowId: '11' };

function hostContext(): WorkbenchHostContext {
  return {
    identity: { projectId: 'project', activeSheetId: sheet.id, routePath: '/p/project', activeRegion: null, activeContributionId: null },
    selection: { selectedRowIds: ['11'], activeRowId: '11', activeCell: null, activeColumnId: null,
      activeSourceId: null, activeEntityId: null, activeEvidenceLinkId: null },
    gridState: { filter: null, sort: null, lensId: null, columnOrder: [], frozenColumns: 0, dataVersion: 0 },
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

function view(panel: WorkbenchPanelDescriptor): WorkbenchViewDescriptor {
  return { ...panel, schemaVersion: 'frisket.workbench.view.v1', kind: 'view' };
}

function projectionApi(): PluginProjectionViewApi {
  return {
    getRuntimeProjectionStatus: vi.fn(), buildRuntimeProjection: vi.fn(),
    readRuntimeProjectionArtifact: vi.fn(), getMapPointsArrowBuffer: vi.fn(),
  };
}

afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe('runtime plugin host assembly', () => {
  it.each(['panel', 'dock', 'peek', 'detail', 'view', 'projection'] as const)(
    'preserves the %s frame attributes, runtime binding and typed context', kind => {
      const panel = descriptor();
      const context = hostContext();
      const common = { descriptor: panel, sheet, hostContext: context };
      const schema = {
        panel: 'plugin_panel', dock: 'plugin_dock_tab', peek: 'plugin_peek', detail: 'plugin_detail',
        view: 'plugin_view', projection: 'plugin_projection_view',
      }[kind];
      const family = kind === 'projection' ? 'view' : kind === 'dock' ? 'dock-tab' : kind;
      const host = { panel: 'rightInspector', dock: 'bottomDock', peek: 'modalOrPeek', detail: 'rowDetail', view: 'mainView', projection: 'mainView' }[kind];
      const focusTab = vi.fn();
      const onDismiss = vi.fn();
      if (kind === 'panel') render(<PluginPanelHost {...common} />);
      else if (kind === 'dock') render(<PluginDockTabHost {...common} isActiveTab focusTab={focusTab} />);
      else if (kind === 'peek') render(<PluginPeekHost {...common} onDismiss={onDismiss} />);
      else if (kind === 'detail') render(<PluginDetailHost {...common} host="rowDetail" subject={subject} />);
      else render(<PluginMainViewHost {...common} descriptor={{ ...view(panel), ...(kind === 'projection' ? { projectionKind: 'timeline' } : {}) }}
        projectId="project" queryRows={async () => ({ rows: [], total: 0 })} openRow={vi.fn()}
        projectionApi={projectionApi()} />);

      const frame = screen.getByTestId('workbench-contribution-plugin-test');
      expect(frame).toHaveAttribute('data-host', host);
      expect(frame).toHaveAttribute(`data-plugin-${family}-status`, 'mounted');
      const schemaAttribute = kind === 'projection' ? 'data-plugin-projection-view-context-schema-version' : `data-plugin-${family}-context-schema-version`;
      expect(frame).toHaveAttribute(schemaAttribute, `frisket.${schema}_context.v1`);
      expect(frame).toHaveAttribute('data-plugin-runtime-module-url', '/plugins/runtime.js');
      expect(frame).toHaveAttribute('data-plugin-runtime-package-sha256', 'package');
      const mounted = runtimeMount.mock.calls.at(-1)![0];
      expect(mounted).toMatchObject({ moduleUrl: '/plugins/runtime.js', componentKey: 'runtime.Component',
        contributionId: 'runtime-contribution', pluginId: 'runtime-plugin', ctx: {
          schemaVersion: `frisket.${schema}_context.v1`, projectId: 'project', contributionId: panel.id,
          sheet: { id: sheet.id }, selection: { selectedRowIds: ['11'], selectedCount: 1 },
        } });
      if (kind === 'detail') expect(mounted.ctx).toMatchObject({ detail: { subject } });
      if (kind === 'dock') expect(mounted.ctx).toMatchObject({ dock: { isActiveTab: true, focus: focusTab } });
      if (kind === 'peek') expect(mounted.ctx).toMatchObject({ peek: { close: onDismiss } });
      if (kind === 'projection') expect(mounted.ctx).toMatchObject({ projection: { kind: 'timeline', target: { dateColumnId: '2' } } });
    },
  );

  it('keeps native panel fallback and host navigation outside runtime mounting', () => {
    const context = hostContext();
    render(<PluginPanelHost descriptor={{ ...descriptor(), runtimeComponent: undefined, componentKey: SELECTION_SUMMARY_PANEL_COMPONENT_KEY }} sheet={sheet} hostContext={context} />);
    expect(screen.getByTestId('plugin-selection-summary-panel')).toHaveAttribute('data-selected-count', '1');
    fireEvent.click(screen.getByTestId('selection-summary-open-first-row'));
    expect(context.navigation.openRow).toHaveBeenCalledWith(sheet.id, '11');
    expect(runtimeMount).not.toHaveBeenCalled();
  });

  it('keeps native gallery and timeline fallback instead of loading a runtime component', () => {
    const context = hostContext();
    const common = { projectId: 'project', sheet, hostContext: context, queryRows: async () => ({ rows: [], total: 0 }), openRow: vi.fn() };
    render(<PluginMainViewHost {...common} descriptor={IMAGE_GALLERY_VIEW_DESCRIPTOR} />);
    render(<PluginMainViewHost {...common} descriptor={{ ...view(descriptor()), runtimeComponent: undefined,
      componentKey: 'trustedLocal.demoTimeline.TimelineView', projectionKind: 'timeline' }} projectionApi={projectionApi()} />);
    expect(screen.getByTestId('native-gallery')).toBeInTheDocument();
    expect(screen.getByTestId('native-timeline')).toBeInTheDocument();
    expect(galleryMount.mock.calls.at(-1)![0].schemaVersion).toBe('frisket.plugin_view_context.v1');
    expect(timelineMount.mock.calls.at(-1)![0].schemaVersion).toBe('frisket.plugin_projection_view_context.v1');
    expect(runtimeMount).not.toHaveBeenCalled();
  });

  it('preserves hidden panel data requirements and explicit dock unavailability', () => {
    const panel = { ...descriptor(), dataRequirements: [{ kind: 'activeRow' as const }] };
    const context = hostContext();
    context.selection.activeRowId = null;
    const hidden = render(<PluginPanelHost descriptor={panel} sheet={sheet} hostContext={context} />);
    expect(hidden.container).toBeEmptyDOMElement();
    render(<PluginDockTabHost descriptor={panel} sheet={sheet} hostContext={context} isActiveTab={false} focusTab={vi.fn()} />);
    expect(screen.getByTestId('plugin-dock-tab-unavailable-plugin-test')).toHaveTextContent('data_requirement_unmet:activeRow');
    expect(runtimeMount).not.toHaveBeenCalled();
  });

  it('never mounts an unavailable capability and retains the peek missing-component policy', () => {
    const panel = descriptor();
    const context = hostContext();
    render(<PluginDetailHost descriptor={{ ...panel, requires: [{ kind: 'hostCapability', id: 'not.offered' }] }}
      sheet={sheet} hostContext={context} host="rowDetail" subject={subject} />);
    expect(screen.getByTestId('plugin-detail-unavailable-plugin-test')).toHaveTextContent('missing_capability:not.offered');
    render(<PluginPeekHost descriptor={{ ...panel, runtimeComponent: undefined }} sheet={sheet} hostContext={context} onDismiss={vi.fn()} />);
    expect(screen.getByTestId('plugin-peek-unavailable')).toHaveTextContent('missing_component');
    expect(runtimeMount).not.toHaveBeenCalled();
  });
});
