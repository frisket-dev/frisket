// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { SkillsSettings } from '../../src/components/SkillsSettings';

const skill = {
  id: 'skill-1', name: 'document-investigation', description: 'Read sources.',
  content: '---\nname: document-investigation\ndescription: Read sources.\n---\n\n# Read\n',
  enabled: true, revision: 1, createdAt: '2026-10-01T00:00:00+00:00', updatedAt: '2026-10-01T00:00:00+00:00',
};

afterEach(() => vi.restoreAllMocks());

it('keeps unsaved SKILL.md text visible when save validation fails', async () => {
  const fetchMock = vi.spyOn(globalThis, 'fetch')
    .mockResolvedValueOnce(new Response(JSON.stringify({ schemaVersion: 'frisket.skills.v1', skills: [skill] })))
    .mockResolvedValueOnce(new Response(JSON.stringify({ detail: 'SKILL.md needs a Markdown instruction body.' }), { status: 422 }));
  render(<SkillsSettings />);
  expect(await screen.findByDisplayValue(skill.content)).toBeVisible();

  const editor = screen.getByTestId('skills-editor');
  fireEvent.change(editor, { target: { value: '---\nname: document-investigation\ndescription: Read sources.\n---\n' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));

  expect(await screen.findByRole('alert')).toHaveTextContent('needs a Markdown instruction body');
  expect(editor).toHaveValue('---\nname: document-investigation\ndescription: Read sources.\n---\n');
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
});
