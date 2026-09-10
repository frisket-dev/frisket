// The single load path for trusted-local plugin frontend exports (React
// components AND command handlers): the module is fetched from its served URL,
// where the backend enforces package integrity (drift fails closed as
// plugin_code_integrity_mismatch), and the export is resolved by its key.

function exportedMemberName(exportKey: string): string {
  const segments = exportKey.split('.').filter(Boolean);
  return segments[segments.length - 1] ?? exportKey;
}

export async function loadTrustedLocalPluginExport(
  moduleUrl: string,
  exportKey: string,
  options: { exact?: boolean } = {},
): Promise<unknown> {
  const moduleExports = (await import(/* @vite-ignore */ moduleUrl)) as Record<
    string,
    unknown
  >;
  const candidate =
    options.exact ? moduleExports[exportKey] : moduleExports[exportKey] ??
    moduleExports[exportedMemberName(exportKey)] ??
    moduleExports.default;
  if (typeof candidate !== 'function') {
    throw new Error(`Missing trusted-local export: ${exportKey}`);
  }
  return candidate;
}
