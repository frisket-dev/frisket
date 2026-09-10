import type { ReactNode } from 'react';

export type PluginHostAvailability =
  | { available: true; reason?: undefined }
  | { available: false; reason: string };

export interface PluginHostShellProps<Descriptor, Sheet> {
  descriptor: Descriptor;
  sheet: Sheet | null;
  resolveAvailability(descriptor: Descriptor, sheet: Sheet | null): PluginHostAvailability;
  renderUnavailable(reason: string): ReactNode;
  mount(sheet: Sheet): ReactNode;
}

export function PluginHostShell<Descriptor, Sheet>({
  descriptor,
  sheet,
  resolveAvailability,
  renderUnavailable,
  mount,
}: PluginHostShellProps<Descriptor, Sheet>): ReactNode {
  const availability = resolveAvailability(descriptor, sheet);
  if (!availability.available) {
    return renderUnavailable(availability.reason);
  }
  if (!sheet) {
    return renderUnavailable('data_requirement_unmet:activeSheet');
  }
  return mount(sheet);
}
