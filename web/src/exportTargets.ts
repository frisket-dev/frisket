import type {
  ActionCatalogEntry,
  ActionCatalogPayload,
  ExportTargetConnectionRequirement,
} from './api/types';

export interface ExportTarget {
  actionKind: string;
  label: string;
  destinationKind: string;
  form?: string;
  sourceModes: string[];
  destinationModes: string[];
  requiresConnection?: ExportTargetConnectionRequirement;
}

function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string' && item.length > 0)
    : [];
}

function destinationKindForEntry(entry: ActionCatalogEntry): string {
  const fallback = entry.kind.split('.').pop();
  return fallback && fallback.length > 0 ? fallback : entry.kind;
}

function exportTargetFromEntry(entry: ActionCatalogEntry): ExportTarget | null {
  const hint = entry.ui_hints?.export_target;
  if (!hint || hint.surface !== 'project.export') return null;
  return {
    actionKind: entry.kind,
    label: hint.label,
    destinationKind: hint.destination_kind || destinationKindForEntry(entry),
    form: hint.form,
    sourceModes: stringList(hint.source_modes),
    destinationModes: stringList(hint.destination_modes),
    requiresConnection: hint.requires_connection,
  };
}

export function exportTargetsFromCatalog(
  catalog: ActionCatalogPayload | null | undefined,
): ExportTarget[] {
  return (catalog?.actions ?? [])
    .map(exportTargetFromEntry)
    .filter((target): target is ExportTarget => target !== null);
}
