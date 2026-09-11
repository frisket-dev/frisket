import { createContext, useContext } from 'react';

export const GuidanceReadyContext = createContext(false);

export function useGuidanceReady(): boolean {
  return useContext(GuidanceReadyContext);
}
