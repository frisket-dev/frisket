import type { HttpInstructionSkill } from '../../generated/openHttpContracts';

export type SkillUploadErrorFactory = (status: number, payload: unknown) => Error;

/** Upload the fixed SKILL.md text endpoint whose OpenAPI operation has no JSON request model. */
export async function uploadSkillMarkdown(
  content: string,
  errorFactory: SkillUploadErrorFactory,
): Promise<HttpInstructionSkill> {
  const response = await fetch('/api/skills/upload', {
    method: 'POST',
    headers: { 'content-type': 'text/markdown; charset=utf-8' },
    body: content,
  });
  if (!response.ok) {
    const payload: unknown = await response.json().catch(() => ({}));
    throw errorFactory(response.status, payload);
  }
  return response.json() as Promise<HttpInstructionSkill>;
}
