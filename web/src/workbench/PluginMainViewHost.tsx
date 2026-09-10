import { useMemo } from 'react';
import type { ColumnDef, SheetDataPage, SheetMeta } from '../api/types';
import { ImageGalleryWorkbenchViewFrame, WorkbenchContributionFrame } from './contributions';
import {
  IMAGE_GALLERY_VIEW_DESCRIPTOR,
  isProjectionViewDescriptor,
  type WorkbenchViewDescriptor,
} from './descriptors';
import { ImageGallery } from './ImageGallery';
import { PluginHostShell } from './pluginHostShell';
import {
  buildPluginProjectionViewContext,
  resolvePluginProjectionViewAvailability,
  type PluginProjectionViewApi,
  type PluginProjectionViewContext,
} from './pluginProjectionViewContext';
import {
  availablePluginViewCapabilities,
  mediaFromPluginViewCell,
  resolvePluginViewAvailability,
  type PluginViewContext,
} from './pluginViewContext';
import { TimelineProjectionView } from './TimelineProjectionView';
import { TrustedLocalPluginComponent } from './TrustedLocalPluginComponent';
import type { WorkbenchHostContext } from './hostContext';
import {
  buildSelectionFragment,
  buildSheetSnapshotFragment,
  optionalActionLaunchSection,
  optionalGridFilterApplySection,
  optionalGridReadSection,
  optionalHostLibrarySection,
} from './pluginContextFragments';

// Host-component registry for projection views whose binding has no module_path:
// the componentKey names a component built into the host. Which component renders
// is a registry lookup; whether a descriptor is a projection view is decided by
// descriptor.projectionKind alone.
const BUILT_IN_PROJECTION_VIEW_COMPONENTS: Record<
  string,
  typeof TimelineProjectionView
> = {
  'trustedLocal.demoTimeline.TimelineView': TimelineProjectionView,
};

export interface PluginMainViewHostProps {
  descriptor: WorkbenchViewDescriptor;
  projectId: string;
  sheet: SheetMeta | null;
  visibleColumns?: ColumnDef[];
  queryRows(args: {
    columnIds?: string[];
    offset: number;
    limit: number;
  }): Promise<SheetDataPage>;
  projectionApi?: PluginProjectionViewApi;
  openRow(rowId: string): void;
  hostContext: WorkbenchHostContext;
  /** Host-routed column scoping for projection views ("open the map for
   *  THIS geo column") — forwarded into projection.params.targetColumnId. */
  targetColumnId?: string;
  /** Host-provided dismissal for host-routed views — becomes
   *  ctx.navigation.closeView on projection view contexts. */
  onCloseView?(): void;
}

type MountedPluginMainViewProps = PluginMainViewHostProps & {
  sheet: SheetMeta;
};

// A data_requirement_unmet view renders NOTHING by design (the sheet simply
// lacks what the view needs). That silence is invisible to a plugin author
// debugging "why doesn't my view show up", so log it ONCE per descriptor+reason
// per page session in dev — loud in dev, still no UI in prod.
const warnedSilentDataRequirements = new Set<string>();

function warnSilentDataRequirementUnmet(
  descriptor: WorkbenchViewDescriptor,
  reason: string,
): void {
  if (!import.meta.env.DEV) return;
  const key = `${descriptor.id}::${reason}`;
  if (warnedSilentDataRequirements.has(key)) return;
  warnedSilentDataRequirements.add(key);
  console.warn(
    `[PluginMainViewHost] view "${descriptor.id}" renders nothing: ${reason} ` +
      '(the active sheet does not satisfy the descriptor dataRequirements). ' +
      'This no-UI outcome is intentional; this notice is dev-only.',
  );
}

function unavailableView(descriptor: WorkbenchViewDescriptor, reason: string) {
  return (
    <WorkbenchContributionFrame
      descriptor={descriptor}
      dataAttributes={{
        'data-plugin-view-status': 'unavailable',
        'data-plugin-view-unavailable-reason': reason,
      }}
    >
      <div
        className="plugin-view-unavailable"
        data-testid={`plugin-view-unavailable-${descriptor.id.replace(/[^a-zA-Z0-9]+/g, '-')}`}
        data-contribution-id={descriptor.id}
        data-reason={reason}
      >
        {reason}
      </div>
    </WorkbenchContributionFrame>
  );
}

function renderUnavailableView(descriptor: WorkbenchViewDescriptor, reason: string) {
  if (reason.startsWith('data_requirement_unmet:')) {
    const syntheticMissingSheet =
      reason === 'data_requirement_unmet:activeSheet' &&
      !descriptor.dataRequirements?.some((requirement) => requirement.kind === 'activeSheet');
    if (!syntheticMissingSheet) warnSilentDataRequirementUnmet(descriptor, reason);
    return null;
  }
  return unavailableView(descriptor, reason);
}

export function PluginMainViewHost(props: PluginMainViewHostProps) {
  const { descriptor, sheet } = props;
  const resolveAvailability = isProjectionViewDescriptor(descriptor)
    ? resolvePluginProjectionViewAvailability
    : (resolvedDescriptor: WorkbenchViewDescriptor, resolvedSheet: SheetMeta | null) =>
        resolvePluginViewAvailability(
          resolvedDescriptor,
          resolvedSheet,
          availablePluginViewCapabilities(),
        );

  return (
    <PluginHostShell
      descriptor={descriptor}
      sheet={sheet}
      resolveAvailability={resolveAvailability}
      renderUnavailable={(reason) => renderUnavailableView(descriptor, reason)}
      mount={(resolvedSheet) => (
        <MountedPluginMainView {...props} sheet={resolvedSheet} />
      )}
    />
  );
}

function MountedPluginMainView({
  descriptor,
  projectId,
  sheet,
  visibleColumns,
  queryRows,
  projectionApi,
  openRow,
  hostContext,
  targetColumnId,
  onCloseView,
}: MountedPluginMainViewProps) {
  const ctxColumns = visibleColumns && visibleColumns.length > 0 ? visibleColumns : sheet.columns;
  const ctx = useMemo<PluginViewContext>(() => ({
    schemaVersion: 'frisket.plugin_view_context.v1',
    projectId,
    contributionId: descriptor.id,
    placement: {
      host: 'mainView',
      mode: 'pane',
    },
    sheet: buildSheetSnapshotFragment(sheet, ctxColumns),
    selection: buildSelectionFragment(hostContext),
    ...optionalGridReadSection(descriptor, hostContext),
    ...optionalGridFilterApplySection(descriptor, hostContext),
    ...optionalActionLaunchSection(descriptor, hostContext),
    ...optionalHostLibrarySection(descriptor),
    rows: {
      query: async (args) => {
        window.__FRISKET_PLUGIN_VIEW_TEST_SPIES__?.rowsQuery?.({
          contributionId: descriptor.id,
          offset: args.offset,
          limit: args.limit,
        });
        return queryRows(args);
      },
    },
    media: {
      fromCell: (value) => {
        window.__FRISKET_PLUGIN_VIEW_TEST_SPIES__?.mediaFromCell?.({
          contributionId: descriptor.id,
          hasValue: value !== null && value !== undefined && value !== '',
        });
        return mediaFromPluginViewCell(value, projectId);
      },
    },
    navigation: {
      openRow: (rowId: string) => {
        window.__FRISKET_PLUGIN_VIEW_TEST_SPIES__?.openRow?.({
          contributionId: descriptor.id,
          rowId,
        });
        openRow(rowId);
      },
    },
  }), [ctxColumns, descriptor, hostContext, openRow, projectId, queryRows, sheet]);

  const projectionCtx = useMemo<PluginProjectionViewContext | null>(() => {
    if (!isProjectionViewDescriptor(descriptor) || !projectionApi) return null;
    return buildPluginProjectionViewContext({
      descriptor,
      projectId,
      sheet,
      projectionApi,
      openRow,
      hostContext,
      targetColumnId,
      onCloseView,
    });
  }, [descriptor, hostContext, onCloseView, openRow, projectId, projectionApi, sheet, targetColumnId]);

  if (isProjectionViewDescriptor(descriptor)) {
    if (!projectionCtx) {
      return unavailableView(descriptor, 'data_requirement_unmet:projectionTarget');
    }
    const projectionDataAttributes = {
      'data-plugin-view-status': 'mounted',
      'data-plugin-projection-view-context-schema-version': projectionCtx.schemaVersion,
      'data-plugin-projection-kind': projectionCtx.projection.kind,
      'data-plugin-projection-target-sheet-id': projectionCtx.projection.target.sheetId,
      'data-plugin-projection-date-column-id':
        projectionCtx.projection.target.dateColumnId,
    };
    if (descriptor.runtimeComponent) {
      return (
        <WorkbenchContributionFrame
          descriptor={descriptor}
          dataAttributes={{
            ...projectionDataAttributes,
            'data-plugin-runtime-module-url': descriptor.runtimeComponent.moduleUrl,
            'data-plugin-runtime-package-sha256': descriptor.runtimeComponent.packageSha256,
          }}
        >
          <TrustedLocalPluginComponent
            moduleUrl={descriptor.runtimeComponent.moduleUrl}
            componentKey={descriptor.runtimeComponent.componentKey}
            contributionId={descriptor.runtimeComponent.contributionId}
            pluginId={descriptor.runtimeComponent.pluginId}
            ctx={projectionCtx}
          />
        </WorkbenchContributionFrame>
      );
    }
    const BuiltInProjectionView = BUILT_IN_PROJECTION_VIEW_COMPONENTS[descriptor.componentKey];
    if (!BuiltInProjectionView) {
      return unavailableView(descriptor, 'missing_component');
    }
    return (
      <WorkbenchContributionFrame descriptor={descriptor} dataAttributes={projectionDataAttributes}>
        <BuiltInProjectionView ctx={projectionCtx} />
      </WorkbenchContributionFrame>
    );
  }

  if (descriptor.runtimeComponent) {
    return (
      <WorkbenchContributionFrame
        descriptor={descriptor}
        dataAttributes={{
          'data-plugin-view-status': 'mounted',
          'data-plugin-view-context-schema-version': ctx.schemaVersion,
          'data-plugin-runtime-module-url': descriptor.runtimeComponent.moduleUrl,
          'data-plugin-runtime-package-sha256': descriptor.runtimeComponent.packageSha256,
        }}
      >
        <TrustedLocalPluginComponent
          moduleUrl={descriptor.runtimeComponent.moduleUrl}
          componentKey={descriptor.runtimeComponent.componentKey}
          contributionId={descriptor.runtimeComponent.contributionId}
          pluginId={descriptor.runtimeComponent.pluginId}
          ctx={ctx}
        />
      </WorkbenchContributionFrame>
    );
  }

  if (descriptor.id !== IMAGE_GALLERY_VIEW_DESCRIPTOR.id) {
    return unavailableView(descriptor, 'missing_component');
  }

  return (
    <ImageGalleryWorkbenchViewFrame
      dataAttributes={{
        'data-plugin-view-status': 'mounted',
        'data-plugin-view-context-schema-version': ctx.schemaVersion,
      }}
    >
      <ImageGallery ctx={ctx} />
    </ImageGalleryWorkbenchViewFrame>
  );
}
