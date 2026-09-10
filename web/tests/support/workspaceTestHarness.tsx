import type { ReactElement, ReactNode } from 'react';
import {
  render as testingLibraryRender,
  type RenderOptions,
} from '@testing-library/react';
import { afterAll } from 'vitest';

import type { ProjectApiPort } from '../../src/api/ports';
import { WorkspaceStoresContext } from '../../src/bind/workspaceStoresContext';
import { createWorkspaceStores } from '../../src/state/createWorkspaceStores';
import { EditionModuleProvider } from '../../src/editions/module';
import { LOCAL_EDITION_MODULE } from '../../src/editions/openModules';

type ExplicitTestApi =
  | { projectApi: ProjectApiPort }
  | { createProjectApi: (projectId: string) => ProjectApiPort };

/** Builds a leaf-component renderer around one explicitly owned project session. */
export function createWorkspaceTestHarness({
  projectId,
  api,
}: {
  projectId: string;
  api: ExplicitTestApi;
}) {
  const projectApi = 'projectApi' in api ? api.projectApi : api.createProjectApi(projectId);
  const stores = createWorkspaceStores(projectId, projectApi);

  const render = (
    ui: ReactElement,
    options: Omit<RenderOptions, 'wrapper'> & Pick<RenderOptions, 'wrapper'> = {},
  ) => {
    const { wrapper: NestedWrapper, ...renderOptions } = options;
    const Wrapper = ({ children }: { children: ReactNode }) => (
      <EditionModuleProvider edition={LOCAL_EDITION_MODULE}>
        <WorkspaceStoresContext.Provider value={stores}>
          {NestedWrapper ? <NestedWrapper>{children}</NestedWrapper> : children}
        </WorkspaceStoresContext.Provider>
      </EditionModuleProvider>
    );
    return testingLibraryRender(ui, { ...renderOptions, wrapper: Wrapper });
  };

  const dispose = () => stores.dispose();
  afterAll(dispose);
  return { projectApi, render, stores, dispose };
}
