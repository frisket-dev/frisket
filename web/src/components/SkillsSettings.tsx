import { useCallback, useEffect, useState } from 'react';
import { FileText, Plus, RefreshCw, Save, Trash2, Upload } from 'lucide-react';

interface InstructionSkill {
  id: string;
  name: string;
  description: string;
  content: string;
  enabled: boolean;
  revision: number;
  createdAt: string;
  updatedAt: string;
}

const starter = `---
name: document-investigation
description: Check representative records, alternatives, coverage, and citations.
---

# Document investigation

Use existing actions, inspect their results, and cite the outputs you use.
`;

async function skillRequest<T>(path: string, init?: RequestInit): Promise<T> {
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

export function SkillsSettings() {
  const [skills, setSkills] = useState<InstructionSkill[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [draft, setDraft] = useState(starter);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const selected = skills.find((skill) => skill.id === selectedId) ?? null;

  const load = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await skillRequest<{ skills: InstructionSkill[] }>('/api/skills');
      setSkills(result.skills);
      setSelectedId((current) => current && result.skills.some((skill) => skill.id === current)
        ? current : (result.skills[0]?.id ?? null));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not load skills.');
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => { if (selected) setDraft(selected.content); }, [selected]);

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const saved = selected
        ? await skillRequest<InstructionSkill>(`/api/skills/${selected.id}`, {
          method: 'PUT', body: JSON.stringify({ expectedRevision: selected.revision, content: draft }),
        })
        : await skillRequest<InstructionSkill>('/api/skills', {
          method: 'POST', body: JSON.stringify({ content: draft, enabled: true }),
        });
      setSkills((current) => selected
        ? current.map((skill) => skill.id === saved.id ? saved : skill)
        : [...current, saved]);
      setSelectedId(saved.id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not save skill.');
    } finally {
      setBusy(false);
    }
  };

  const toggle = async () => {
    if (!selected) return;
    setBusy(true);
    setError(null);
    try {
      const saved = await skillRequest<InstructionSkill>(`/api/skills/${selected.id}/enabled`, {
        method: 'PATCH', body: JSON.stringify({ expectedRevision: selected.revision, enabled: !selected.enabled }),
      });
      setSkills((current) => current.map((skill) => skill.id === saved.id ? saved : skill));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not update skill.');
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!selected) return;
    setBusy(true);
    setError(null);
    try {
      await skillRequest(`/api/skills/${selected.id}`, {
        method: 'DELETE', body: JSON.stringify({ expectedRevision: selected.revision }),
      });
      setSkills((current) => current.filter((skill) => skill.id !== selected.id));
      setSelectedId(null);
      setDraft(starter);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not remove skill.');
    } finally {
      setBusy(false);
    }
  };

  const upload = async (file: File | undefined) => {
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const content = await file.text();
      const saved = await skillRequest<InstructionSkill>('/api/skills/upload', {
        method: 'POST', headers: { 'content-type': 'text/markdown; charset=utf-8' }, body: content,
      });
      setSkills((current) => [...current, saved]);
      setSelectedId(saved.id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not upload SKILL.md.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="settings-section" data-testid="skills-settings">
      <div className="settings-section-head">
        <div>
          <h2><FileText size={16} aria-hidden /> Skills</h2>
          <p>Text-only instructions for Ask. Skills cannot run code or add tools.</p>
        </div>
        <div className="settings-actions">
          <button type="button" className="icon-btn" aria-label="Refresh skills" onClick={() => void load()} disabled={busy}><RefreshCw size={14} /></button>
          <label className="btn"><Upload size={14} /> Upload SKILL.md<input data-testid="skills-upload" type="file" accept="text/markdown,text/plain,.md" hidden onChange={(event) => void upload(event.currentTarget.files?.[0])} /></label>
          <button type="button" className="btn" onClick={() => { setSelectedId(null); setDraft(starter); }} disabled={busy}><Plus size={14} /> New skill</button>
        </div>
      </div>
      {error && <p className="settings-error" role="alert">{error}</p>}
      <div className="settings-split">
        <div className="settings-list" aria-label="Saved skills">
          {skills.map((skill) => <button key={skill.id} type="button" className={`settings-list-item${skill.id === selectedId ? ' selected' : ''}`} onClick={() => setSelectedId(skill.id)}><strong>{skill.name}</strong><span>{skill.enabled ? 'Enabled' : 'Disabled'}</span></button>)}
          {skills.length === 0 && <p className="muted">No skills saved yet.</p>}
        </div>
        <div className="settings-editor">
          <label htmlFor="skill-content">SKILL.md</label>
          <textarea id="skill-content" className="form-input" data-testid="skills-editor" value={draft} rows={16} onChange={(event) => setDraft(event.currentTarget.value)} disabled={busy} />
          <div className="settings-actions">
            <button type="button" className="btn btn-accept" onClick={() => void save()} disabled={busy}><Save size={14} /> Save</button>
            {selected && <button type="button" className="btn" onClick={() => void toggle()} disabled={busy}>{selected.enabled ? 'Disable' : 'Enable'}</button>}
            {selected && <button type="button" className="btn btn-reject" onClick={() => void remove()} disabled={busy}><Trash2 size={14} /> Delete</button>}
          </div>
        </div>
      </div>
    </section>
  );
}
