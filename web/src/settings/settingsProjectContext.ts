import type { ProjectInfo } from '../api/open';

const SETTINGS_LAST_PROJECT_KEY = 'frisket.settings.last_project.v1';
const SETTINGS_ACTIVE_PROJECT_CONTEXT_KEY = 'frisket.settings.active_project.v1';

export function clearSettingsProjectContext(): void {
  try {
    sessionStorage.removeItem(SETTINGS_ACTIVE_PROJECT_CONTEXT_KEY);
  } catch {
    // Ignore storage access failures; route rendering remains authoritative.
  }
}

export function writeSettingsProjectContext(project: ProjectInfo): void {
  try {
    const payload = JSON.stringify({
      id: project.id,
      name: project.name,
      description: project.description ?? '',
      sensitive: Boolean(project.sensitive),
      role: project.role ?? null,
    });
    localStorage.setItem(SETTINGS_LAST_PROJECT_KEY, payload);
    sessionStorage.setItem(SETTINGS_ACTIVE_PROJECT_CONTEXT_KEY, project.id);
  } catch {
    // Ignore storage access failures; route rendering remains authoritative.
  }
}

export function readSettingsProjectContext(): ProjectInfo | null {
  try {
    const activeProjectId = sessionStorage.getItem(SETTINGS_ACTIVE_PROJECT_CONTEXT_KEY);
    if (!activeProjectId) return null;
    const raw = localStorage.getItem(SETTINGS_LAST_PROJECT_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (
      parsed &&
      parsed.id === activeProjectId &&
      typeof parsed.id === 'string' &&
      typeof parsed.name === 'string'
    ) {
      return {
        id: parsed.id,
        name: parsed.name,
        description: typeof parsed.description === 'string' ? parsed.description : '',
        sensitive: Boolean(parsed.sensitive),
        role: typeof parsed.role === 'string' ? parsed.role : null,
      };
    }
  } catch {
    return null;
  }
  return null;
}
