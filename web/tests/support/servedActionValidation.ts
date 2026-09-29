import { spawnSync } from 'node:child_process';
import { delimiter, resolve } from 'node:path';

import type { ActionParamResolution, GeneratedActionRequest } from '../../src/api/types';
import { servedActionCatalogPython } from './servedActionCatalog';

type ParamValidationRequest = Pick<GeneratedActionRequest, 'action_id' | 'scope' | 'params'>;

/** Validate a bounded request matrix through the real Python service once. */
export function servedActionValidations(
  requests: readonly ParamValidationRequest[],
): ActionParamResolution[] {
  const repositoryRoot = resolve(process.cwd(), '..');
  const sourcePath = resolve(repositoryRoot, 'src');
  const pythonPath = process.env.PYTHONPATH
    ? `${sourcePath}${delimiter}${process.env.PYTHONPATH}`
    : sourcePath;
  const result = spawnSync(servedActionCatalogPython(), ['-c', `
import json, sys, tempfile
from pathlib import Path
from types import SimpleNamespace
from frisket.engine.store import Project
from frisket.server.services.action_param_validation import ActionParamValidationService

with tempfile.TemporaryDirectory() as tmp:
    project = Project.create(Path(tmp) / "validation.frisket")
    try:
        service = ActionParamValidationService(
            SimpleNamespace(edition="solo", get=lambda _: project)
        )
        print(json.dumps([
            service.validate_params("validation-project", request)
            for request in json.load(sys.stdin)
        ]))
    finally:
        project.close()
`], {
    cwd: repositoryRoot,
    input: JSON.stringify(requests),
    encoding: 'utf8',
    env: {
      ...process.env,
      FRISKET_CHECKLOG_DISABLE: '1',
      PYTHONPATH: pythonPath,
    },
    maxBuffer: 4 * 1024 * 1024,
  });
  if (result.status !== 0 || !result.stdout) {
    throw new Error(
      `Backend action validation failed: ${result.error?.message ?? result.stderr}`,
    );
  }
  return JSON.parse(result.stdout) as ActionParamResolution[];
}
