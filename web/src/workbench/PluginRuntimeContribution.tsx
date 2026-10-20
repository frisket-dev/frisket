import type { ComponentProps } from 'react';
import { WorkbenchContributionFrame } from './contributions';
import type { WorkbenchContributionDescriptor } from './descriptors';
import { TrustedLocalPluginComponent } from './TrustedLocalPluginComponent';

type PluginRuntimeContributionProps = Omit<ComponentProps<typeof WorkbenchContributionFrame>, 'children'> & {
  runtime: NonNullable<WorkbenchContributionDescriptor['runtimeComponent']>;
  ctx: ComponentProps<typeof TrustedLocalPluginComponent>['ctx'];
};

/** Mount an admitted runtime binding; each host still builds its own typed context and fallback. */
export function PluginRuntimeContribution({ runtime, ctx, dataAttributes, ...frame }: PluginRuntimeContributionProps) {
  return (
    <WorkbenchContributionFrame {...frame} dataAttributes={{
      ...dataAttributes,
      'data-plugin-runtime-module-url': runtime.moduleUrl,
      'data-plugin-runtime-package-sha256': runtime.packageSha256,
    }}>
      <TrustedLocalPluginComponent
        moduleUrl={runtime.moduleUrl}
        componentKey={runtime.componentKey}
        contributionId={runtime.contributionId}
        pluginId={runtime.pluginId}
        ctx={ctx}
      />
    </WorkbenchContributionFrame>
  );
}
