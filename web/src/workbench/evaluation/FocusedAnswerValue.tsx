import { useCallback, useEffect, useMemo } from 'react';
import { BoundedTextReader } from '../../components/BoundedTextReader';

/** Render a focused full answer with the same bounded reader as Inspect. */
export function FocusedAnswerValue({ text }: { text: string }) {
  const characters = useMemo(() => Array.from(text), [text]);
  const bytes = useMemo(() => new TextEncoder().encode(text).byteLength, [text]);
  const url = useMemo(() => URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' })), [text]);
  useEffect(() => () => URL.revokeObjectURL(url), [url]);
  const loadWindow = useCallback(async ({ start, limit }: { start: number; limit: number }) => {
    const end = Math.min(characters.length, start + limit);
    return { retrieval_revision: 1, total_characters: characters.length, total_bytes: bytes,
      range: { start, end }, text: characters.slice(start, end).join(''),
      previous_start: start > 0 ? Math.max(0, start - limit) : null,
      next_start: end < characters.length ? end : null };
  }, [characters, bytes]);
  return <BoundedTextReader totalCharacters={characters.length} totalBytes={bytes} loadWindow={loadWindow}
    downloadUrl={() => url} copyFullValue={async () => text} />;
}
