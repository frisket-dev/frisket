import { useEffect, useState } from 'react';
import type { Theme } from '@glideapps/glide-data-grid';

import {
  readGeneratedColumnTheme,
  readGridCellPalette,
  readGridDifferenceTheme,
  readExpectedColumnTheme,
  readGridTheme,
  readRowFilterTints,
  type GridCellPalette,
  type RowFilterTints,
} from './gridTheme';

interface GridThemeSnapshot {
  gridTheme: Partial<Theme>;
  generatedColumnTheme: Partial<Theme>;
  expectedColumnTheme: Partial<Theme>;
  gridCellPalette: GridCellPalette;
  differenceTheme: Partial<Theme>;
  rowFilterTints: RowFilterTints;
}

function readGridThemeSnapshot(): GridThemeSnapshot {
  return {
    gridTheme: readGridTheme(),
    generatedColumnTheme: readGeneratedColumnTheme(),
    expectedColumnTheme: readExpectedColumnTheme(),
    gridCellPalette: readGridCellPalette(),
    differenceTheme: readGridDifferenceTheme(),
    rowFilterTints: readRowFilterTints(),
  };
}

/** One reactive CSS-token snapshot for every canvas grid surface. */
export function useGridTheme(): GridThemeSnapshot {
  const [snapshot, setSnapshot] = useState(readGridThemeSnapshot);

  useEffect(() => {
    const sync = () => {
      const next = readGridThemeSnapshot();
      setSnapshot(next);
      if (import.meta.env.DEV) {
        (window as unknown as { __frisketGridTheme?: Partial<Theme> }).__frisketGridTheme =
          next.gridTheme;
      }
    };
    sync();
    const observer = new MutationObserver(sync);
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['data-frisket-theme'],
    });
    return () => observer.disconnect();
  }, []);

  return snapshot;
}
