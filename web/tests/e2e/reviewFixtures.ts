import { execFileSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';

type ReviewClassifyField = {
  description?: string;
  labels: string[];
  name: string;
  type: string;
};

const REPO_ROOT =
  path.basename(process.cwd()) === 'web'
    ? path.resolve(process.cwd(), '..')
    : process.cwd();

export function seedReviewClassifyRun({
  context,
  fields,
  sourceColumns,
  pid,
  reply,
  sheetId,
}: {
  context: string;
  fields: ReviewClassifyField[];
  sourceColumns: string[];
  pid: string;
  reply: Record<string, unknown>;
  sheetId: number;
}): void {
  const workspace = process.env.FRISKET_E2E_WS ?? path.join(os.homedir(), '.frisket/e2e-ws');
  const includes = (suffix: string) => fields.some(({ name }) => (
    Object.prototype.hasOwnProperty.call(reply, `${name}_${suffix}`)
  ));
  const payload = JSON.stringify({
    reply,
    action: {
      action_id: 'map.classify',
      scope: { kind: 'sheet_rows', sheet_id: sheetId },
      params: {
        source: sourceColumns,
        engine: 'llm',
        model: 'anthropic/claude-haiku-4-5',
        context,
        fields,
        include_justification: includes('justification'),
        include_confidence: includes('confidence'),
      },
      idempotency_key: 'review-e2e-classify@sha256:v1',
    },
  });
  const script = String.raw`
import json
import sys
from pathlib import Path

from frisket.ai.llm import LLMRequest, LLMResponse, ModelRouter
from frisket.engine.executor import run_action_spec
from frisket.engine.store import Project


class StubAdapter:
    def __init__(self, reply):
        self.reply = reply

    async def complete(self, req: LLMRequest, client) -> LLMResponse:  # noqa: ANN001
        return LLMResponse(
            content=json.dumps(self.reply),
            data=dict(self.reply),
            tokens_in=10,
            tokens_out=10,
            cost=0.0,
            model=req.model,
        )


workspace, pid, payload_json = sys.argv[1], sys.argv[2], sys.argv[3]
payload = json.loads(payload_json)
router = ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off")
router._adapters["anthropic"] = StubAdapter(payload["reply"])
project = Project(Path(workspace) / f"{pid}.frisket")
try:
    result = run_action_spec(
        project,
        payload["action"],
        project_id=pid,
        router=router,
    )
    if result.status != "completed":
        raise RuntimeError(result.errors)
finally:
    project.close()
`;
  execFileSync('uv', ['run', 'python', '-c', script, workspace, pid, payload], {
    cwd: REPO_ROOT,
    stdio: 'pipe',
    timeout: 60_000,
  });
}
