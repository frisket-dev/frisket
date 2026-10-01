export interface InstructionSkill {
  id: string;
  name: string;
  description: string;
  content: string;
  enabled: boolean;
  revision: number;
  createdAt: string;
  updatedAt: string;
}

interface SkillsEnvelope {
  schemaVersion: 'frisket.skills.v1';
  skills: InstructionSkill[];
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { 'content-type': 'application/json', ...init?.headers },
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const detail = Array.isArray(payload.detail) ? payload.detail[0]?.msg : payload.detail;
    throw new Error(typeof detail === 'string' ? detail : 'Could not save skills.');
  }
  return response.status === 204 ? undefined as T : response.json() as Promise<T>;
}

export const skillsApi = {
  list: () => request<SkillsEnvelope>('/api/skills'),
  create: (content: string) => request<InstructionSkill>('/api/skills', {
    method: 'POST', body: JSON.stringify({ content, enabled: true }),
  }),
  upload: (content: string) => request<InstructionSkill>('/api/skills/upload', {
    method: 'POST', headers: { 'content-type': 'text/markdown; charset=utf-8' }, body: content,
  }),
  update: (skill: InstructionSkill, content: string) => request<InstructionSkill>(`/api/skills/${skill.id}`, {
    method: 'PUT', body: JSON.stringify({ expectedRevision: skill.revision, content }),
  }),
  setEnabled: (skill: InstructionSkill, enabled: boolean) => request<InstructionSkill>(`/api/skills/${skill.id}/enabled`, {
    method: 'PATCH', body: JSON.stringify({ expectedRevision: skill.revision, enabled }),
  }),
  delete: (skill: InstructionSkill) => request<void>(`/api/skills/${skill.id}`, {
    method: 'DELETE', body: JSON.stringify({ expectedRevision: skill.revision }),
  }),
};
