import type {
  WorkbenchPluginFrontendComponentBinding,
  WorkbenchPluginRuntimePlugin,
} from '../api/types';

const FRONTEND_COMPONENT_BINDING_SCHEMA_VERSION =
  'frisket.workbench_plugin_frontend_component_binding.v1';

export function frontendBindingForContribution(
  plugin: WorkbenchPluginRuntimePlugin | null,
  contributionId: string,
): WorkbenchPluginFrontendComponentBinding | null {
  if (!plugin || plugin.arbitraryPackageLoadAllowed) return null;
  const bindings = plugin.frontendComponentBindings ?? [];
  return (
    bindings.find(
      (binding) =>
        binding.schemaVersion === FRONTEND_COMPONENT_BINDING_SCHEMA_VERSION &&
        binding.contributionId === contributionId &&
        binding.moduleKey.length > 0 &&
        binding.componentKey.length > 0,
    ) ?? null
  );
}

export function formatFrontendComponentBindings(
  plugin: WorkbenchPluginRuntimePlugin | null,
): string | undefined {
  const bindings = plugin?.frontendComponentBindings ?? [];
  if (bindings.length === 0) return undefined;
  const formattedBindings: string[] = [];
  for (const binding of bindings) {
    if (binding.schemaVersion !== FRONTEND_COMPONENT_BINDING_SCHEMA_VERSION) continue;
    formattedBindings.push(`${binding.contributionId}=${binding.moduleKey}#${binding.componentKey}`);
  }
  return formattedBindings.join(' ');
}
