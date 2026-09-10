import { createContext, useContext } from 'react';

export const OpenLensContext = createContext<((lensId: number, name: string) => void) | null>(
  null,
);

export function useOpenLens() {
  const openLens = useContext(OpenLensContext);
  if (!openLens) {
    throw new Error('LensList must be rendered inside OpenLensContext.Provider');
  }
  return openLens;
}
