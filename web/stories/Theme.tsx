import { useEffect, type ReactNode } from 'react';

export function Theme({ theme, children }: { theme: 'light' | 'dark'; children: ReactNode }) {
  useEffect(() => {
    document.documentElement.dataset.frisketTheme = theme;
    return () => { delete document.documentElement.dataset.frisketTheme; };
  }, [theme]);
  return children;
}
