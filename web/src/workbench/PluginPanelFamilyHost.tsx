import { useMemo, type ReactNode } from 'react';
import type { SheetMeta } from '../api/types';
import { WorkbenchContributionFrame } from './contributions';
import type { WorkbenchHostId, WorkbenchPanelDescriptor } from './descriptors';
import type { WorkbenchHostContext } from './hostContext';
import { PluginHostShell } from './pluginHostShell';
import { resolvePluginPanelAvailability } from './pluginPanelContext';
import { PluginRuntimeContribution } from './PluginRuntimeContribution';
import { contributionSlug } from './visibility';

type PluginPanelFamily = 'detail' | 'dock-tab' | 'panel' | 'peek';
type DataAttributes = Record<string, string>;
type RuntimeContext = { schemaVersion: string };

interface PluginPanelFamilyHostProps<Ctx extends RuntimeContext> {
  /** Names the `data-plugin-<family>-*` frame attributes and unavailable test id. */
  family: PluginPanelFamily;
  descriptor: WorkbenchPanelDescriptor;
  sheet: SheetMeta | null;
  hostContext: WorkbenchHostContext;
  host: WorkbenchHostId;
  className: string;
  /** Class of the default unavailable body. */
  unavailableClassName?: string;
  /** Replaces the default unavailable body. */
  renderUnavailableBody?(reason: string): ReactNode;
  /** Extra frame attributes in the unavailable state, before the reason. */
  unavailableAttributes?: DataAttributes;
  /** Reasons that render nothing instead of an unavailable frame. */
  hidesReason?(reason: string): boolean;
  /** Must be referentially stable while its inputs are unchanged; the context is memoised on it. */
  buildContext(sheet: SheetMeta): Ctx;
  /** Extra frame attributes in the mounted state, after the context schema version. */
  mountedAttributes?(ctx: Ctx): DataAttributes;
  /** Rendered for a descriptor without a runtime component; null falls back to `missing_component`. */
  renderWithoutRuntime?(ctx: Ctx, mounted: DataAttributes): ReactNode;
}

/** One skeleton for the panel-shaped plugin hosts: availability gate, unavailable frame, typed context, runtime mount. */
export function PluginPanelFamilyHost<Ctx extends RuntimeContext>(props: PluginPanelFamilyHostProps<Ctx>) {
  const { descriptor, sheet, hostContext, hidesReason } = props;
  return (
    <PluginHostShell
      descriptor={descriptor}
      sheet={sheet}
      resolveAvailability={(resolvedDescriptor, resolvedSheet) =>
        resolvePluginPanelAvailability(resolvedDescriptor, resolvedSheet, hostContext)
      }
      renderUnavailable={(reason) => (hidesReason?.(reason) ? null : <UnavailableFrame {...props} reason={reason} />)}
      mount={(resolvedSheet) => <MountedFamilyHost {...props} sheet={resolvedSheet} />}
    />
  );
}

function UnavailableFrame<Ctx extends RuntimeContext>({
  family,
  descriptor,
  host,
  className,
  unavailableClassName,
  renderUnavailableBody,
  unavailableAttributes,
  reason,
}: PluginPanelFamilyHostProps<Ctx> & { reason: string }) {
  return (
    <WorkbenchContributionFrame
      descriptor={descriptor}
      host={host}
      className={className}
      dataAttributes={{
        [`data-plugin-${family}-status`]: 'unavailable',
        ...unavailableAttributes,
        [`data-plugin-${family}-unavailable-reason`]: reason,
      }}
    >
      {renderUnavailableBody ? renderUnavailableBody(reason) : (
        <div
          className={unavailableClassName}
          data-testid={`plugin-${family}-unavailable-${contributionSlug(descriptor.id)}`}
          data-contribution-id={descriptor.id}
          data-reason={reason}
        >
          {reason}
        </div>
      )}
    </WorkbenchContributionFrame>
  );
}

function MountedFamilyHost<Ctx extends RuntimeContext>(props: PluginPanelFamilyHostProps<Ctx> & { sheet: SheetMeta }) {
  const { family, descriptor, sheet, host, className, buildContext, mountedAttributes, renderWithoutRuntime } = props;
  const ctx = useMemo(() => buildContext(sheet), [buildContext, sheet]);
  const mounted: DataAttributes = {
    [`data-plugin-${family}-status`]: 'mounted',
    [`data-plugin-${family}-context-schema-version`]: ctx.schemaVersion,
    ...mountedAttributes?.(ctx),
  };

  if (descriptor.runtimeComponent) {
    return (
      <PluginRuntimeContribution
        descriptor={descriptor}
        host={host}
        className={className}
        dataAttributes={mounted}
        runtime={descriptor.runtimeComponent}
        ctx={ctx}
      />
    );
  }
  return renderWithoutRuntime?.(ctx, mounted) ?? <UnavailableFrame {...props} reason="missing_component" />;
}
