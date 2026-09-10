import { createContext, useContext } from 'react';

import type { EditionModule } from './module';

export const EditionModuleContext = createContext<Readonly<EditionModule> | null>(null);

export function useEditionModule(): Readonly<EditionModule> {
  const edition = useContext(EditionModuleContext);
  if (!edition) throw new Error('EditionModuleProvider is missing');
  return edition;
}
