import './PluginActionUIBoundary.css';
import * as React from 'react';
import type { ReactNode } from 'react';
import type { GeneratedActionCatalogEntry } from '../../api/types';
import { loadTrustedLocalPluginExport } from '../../workbench/trustedLocalModule';
import { TrustedLocalContributionErrorBoundary } from '../../workbench/TrustedLocalPluginComponent';
import type { GeneratedActionCustomization } from './generatedActionCustomizations';
import { useLoader } from '../../hooks/useLoader';

function validatedUI(value: unknown, entry: GeneratedActionCatalogEntry): GeneratedActionCustomization {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Invalid action UI');
  const ui = value as Record<string, unknown>;
  if (Object.getPrototypeOf(ui) !== Object.prototype && Object.getPrototypeOf(ui) !== null) throw new Error('Invalid action UI');
  if (Object.keys(ui).some((key) => key !== 'fields' && key !== 'body')) throw new Error('Invalid action UI');
  if (ui.body !== undefined && typeof ui.body !== 'function') throw new Error('Invalid Params body');
  if (ui.fields !== undefined) {
    if (!ui.fields || typeof ui.fields !== 'object' || Array.isArray(ui.fields)) throw new Error('Invalid fields');
    for (const [name, control] of Object.entries(ui.fields)) {
      if (!Object.hasOwn(entry.input_schema.properties ?? {}, name) || typeof control !== 'function') {
        throw new Error('Invalid field override');
      }
    }
  }
  return ui as GeneratedActionCustomization;
}

/** The parent keys this boundary by the complete installed editor identity. */
export function PluginActionUIBoundary({ entry, projectId, children }: {
  entry: GeneratedActionCatalogEntry;
  projectId: string;
  children(ui: GeneratedActionCustomization): ReactNode;
}) {
  const binding = entry.ui_hints.action_ui!;
  const moduleUrl = `/api/projects/${encodeURIComponent(projectId)}/workbench/plugins/${encodeURIComponent(binding.plugin_id)}`
    + `/frontend-components/${encodeURIComponent(entry.kind)}/module.js?package=${encodeURIComponent(binding.package_sha256)}`;
  const loader = useLoader(async (signal) => {
    const factory = await loadTrustedLocalPluginExport(moduleUrl, binding.export_name, { exact: true });
    // A superseded module must never run its factory.
    if (signal.aborted) throw signal.reason;
    // Factories are hook-free; only their returned React components render.
    return validatedUI((factory as (host: { React: typeof React }) => unknown)({ React }), entry);
  }, [moduleUrl, binding.export_name, entry], { keepPreviousData: true });
  if (loader.error !== null) return <p role="alert">The installed action UI could not load. Rebuild or reinstall the plugin.</p>;
  if (!loader.data) return <p role="status">Loading action editor…</p>;
  return <TrustedLocalContributionErrorBoundary contributionId={entry.kind}
    pluginId={binding.plugin_id} moduleUrl={moduleUrl}>{children(loader.data)}</TrustedLocalContributionErrorBoundary>;
}
