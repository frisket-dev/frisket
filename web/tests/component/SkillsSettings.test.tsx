// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { SkillsSettings } from '../../src/components/SkillsSettings';

const skill = {
  id: 'skill-1', name: 'document-investigation', description: 'Read sources.',
  content: '---\nname: document-investigation\ndescription: Read sources.\n---\n\n# Read\n',
  enabled: true, revision: 1, createdAt: '2026-10-01T00:00:00+00:00', updatedAt: '2026-10-01T00:00:00+00:00',
};

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it('keeps unsaved SKILL.md text visible when save validation fails', async () => {
  const fetchMock = vi.spyOn(globalThis, 'fetch')
    .mockResolvedValueOnce(new Response(JSON.stringify({ schemaVersion: 'frisket.skills.v1', skills: [skill] })))
    .mockResolvedValueOnce(new Response(JSON.stringify({ detail: 'SKILL.md needs a Markdown instruction body.' }), { status: 422 }));
  render(<SkillsSettings />);
  const editor = screen.getByTestId('skills-editor');
  await waitFor(() => expect(editor).toHaveValue(skill.content));

  fireEvent.change(editor, { target: { value: '---\nname: document-investigation\ndescription: Read sources.\n---\n' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));

  expect(await screen.findByRole('alert')).toHaveTextContent('needs a Markdown instruction body');
  expect(editor).toHaveValue('---\nname: document-investigation\ndescription: Read sources.\n---\n');
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
});


it('keeps an admitted upload when a stale initial list resolves afterwards', async () => {
  let resolveList!: (response: Response) => void;
  const list = new Promise<Response>((resolve) => { resolveList = resolve; });
  const uploaded = { ...skill, id: 'uploaded' };
  vi.spyOn(globalThis, 'fetch')
    .mockImplementationOnce(() => list)
    .mockResolvedValueOnce(new Response(JSON.stringify(uploaded)));

  render(<SkillsSettings />);
  const input = screen.getByTestId('skills-upload');
  await waitFor(() => expect(input).toBeDisabled());

  // A change event queued before the disabled state commits must still not let
  // the old list response erase the admitted skill.
  fireEvent.change(input, { target: { files: [{ text: async () => skill.content }] } });
  await waitFor(() => expect(screen.getByText('document-investigation')).toBeVisible());
  resolveList(new Response(JSON.stringify({ schemaVersion: 'frisket.skills.v1', skills: [] })));
  await waitFor(() => expect(screen.getByText('document-investigation')).toBeVisible());
  expect(input).not.toBeDisabled();
});

it('does not start a second upload while the first upload is pending', async () => {
  let resolveUpload!: (response: Response) => void;
  const upload = new Promise<Response>((resolve) => { resolveUpload = resolve; });
  vi.spyOn(globalThis, 'fetch')
    .mockResolvedValueOnce(new Response(JSON.stringify({ schemaVersion: 'frisket.skills.v1', skills: [] })))
    .mockImplementationOnce(() => upload);

  render(<SkillsSettings />);
  const input = screen.getByTestId('skills-upload');
  await waitFor(() => expect(input).not.toBeDisabled());
  fireEvent.change(input, { target: { files: [{ text: async () => skill.content }] } });
  fireEvent.change(input, { target: { files: [{ text: async () => skill.content }] } });
  await waitFor(() => expect(input).toBeDisabled());
  expect(globalThis.fetch).toHaveBeenCalledTimes(2);

  resolveUpload(new Response(JSON.stringify(skill)));
  await waitFor(() => expect(screen.getByText('document-investigation')).toBeVisible());
});
