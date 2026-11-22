import { createContext, useContext, useLayoutEffect, useRef } from 'react';

export type RightSheetLookup = (name: string) => number | null;
export type RegisterRightSheetLookup = (lookup: RightSheetLookup | null) => void;

export const JoinRightSheetLookupContext = createContext<RegisterRightSheetLookup | null>(null);

export function useRegisterRightSheetLookup(lookup: RightSheetLookup): void {
  const register = useContext(JoinRightSheetLookupContext);
  const lookupRef = useRef(lookup);
  useLayoutEffect(() => {
    lookupRef.current = lookup;
  });
  useLayoutEffect(() => {
    if (!register) return undefined;
    register((name) => lookupRef.current(name));
    return () => register(null);
  }, [register]);
}
