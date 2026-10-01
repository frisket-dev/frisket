import { httpContract } from "./httpContract";
import type {
  HttpInstructionSkill,
  HttpInstructionSkillList,
} from "../generated/openHttpContracts";
import { uploadSkillMarkdown } from "./raw/skillMarkdownUpload";

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
  upload: (content: string): Promise<InstructionSkill> =>
    uploadSkillMarkdown(content, skillRequestError),
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
