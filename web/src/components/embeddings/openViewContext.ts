import { createContext, useContext } from 'react';

export const OpenViewContext = createContext<((viewId: number, name: string) => void) | null>(
  null,
);

export function useOpenView() {
  const openView = useContext(OpenViewContext);
  if (!openView) {
    throw new Error('ViewList must be rendered inside OpenViewContext.Provider');
  }
  return openView;
}
