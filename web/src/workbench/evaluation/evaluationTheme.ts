import type { Theme } from '@glideapps/glide-data-grid';

/** Canvas evaluation marks share the ordinary semantic CSS colors. */
export function evaluationTheme(theme: Partial<Theme>) {
  const styles = typeof document === 'undefined' ? null : getComputedStyle(document.documentElement);
  const token = (name: string, fallback: string | undefined) => styles?.getPropertyValue(name).trim() || fallback || 'black';
  return { match: token('--green', theme.textDark), expected: token('--green-bg', theme.bgCellMedium), miss: token('--red-bg', theme.bgCellMedium) };
}
