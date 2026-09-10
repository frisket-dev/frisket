import type { ReactNode } from 'react';

import type { EditionModule } from './module';
import { EditionModuleContext } from './moduleContext';

export function EditionModuleProvider({
  children,
  edition,
}: {
  children: ReactNode;
  edition: Readonly<EditionModule>;
}) {
  return (
    <EditionModuleContext.Provider value={edition}>
      {children}
    </EditionModuleContext.Provider>
  );
}
