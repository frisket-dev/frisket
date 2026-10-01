import { httpContract } from "./httpContract";
import type {
  HttpInstructionSkill,
  HttpInstructionSkillList,
} from "../generated/openHttpContracts";

export type InstructionSkill = HttpInstructionSkill;
export type SkillsEnvelope = HttpInstructionSkillList;

function skillRequestError(_status: number, payload: unknown): Error {
  const detail =
    typeof payload === "object" && payload !== null && "detail" in payload
      ? payload.detail
      : undefined;
  const message = Array.isArray(detail) ? detail[0]?.msg : detail;
  return new Error(
    typeof message === "string" ? message : "Could not save skills.",
  );
}

async function uploadSkill(content: string): Promise<InstructionSkill> {
  const response = await fetch("/api/skills/upload", {
    method: "POST",
    headers: { "content-type": "text/markdown; charset=utf-8" },
    body: content,
  });
  if (!response.ok) {
    const payload: unknown = await response.json().catch(() => ({}));
    throw skillRequestError(response.status, payload);
  }
  return response.json() as Promise<InstructionSkill>;
}

export const skillsApi = {
  list: (): Promise<SkillsEnvelope> =>
    httpContract("tenant.list_skills.get", {
      pathParams: {},
      query: {},
      errorFactory: skillRequestError,
    }),
  create: (content: string): Promise<InstructionSkill> =>
    httpContract("tenant.create_skill.post", {
      pathParams: {},
      query: {},
      body: { content, enabled: true },
      errorFactory: skillRequestError,
    }),
  // Upload remains a raw text request because its contract intentionally
  // declares no JSON or multipart body; the route accepts SKILL.md bytes.
  upload: uploadSkill,
  update: (
    skill: InstructionSkill,
    content: string,
  ): Promise<InstructionSkill> =>
    httpContract("tenant.update_skill.put", {
      pathParams: { skill_id: skill.id },
      query: {},
      body: { expectedRevision: skill.revision, content },
      errorFactory: skillRequestError,
    }),
  setEnabled: (
    skill: InstructionSkill,
    enabled: boolean,
  ): Promise<InstructionSkill> =>
    httpContract("tenant.set_skill_enabled.patch", {
      pathParams: { skill_id: skill.id },
      query: {},
      body: { expectedRevision: skill.revision, enabled },
      errorFactory: skillRequestError,
    }),
  delete: (skill: InstructionSkill): Promise<void> =>
    httpContract("tenant.delete_skill.delete", {
      pathParams: { skill_id: skill.id },
      query: {},
      body: { expectedRevision: skill.revision },
      errorFactory: skillRequestError,
    }),
};
