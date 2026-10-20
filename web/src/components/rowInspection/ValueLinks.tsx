import './FieldValue.css';
import type { ReactNode } from 'react';

const URL_RE = /https?:\/\/[^\s"'<>)\]}]+/g;

export function safeHttpUrl(raw: string): string | null {
  const url = raw.trim();
  if (!/^https?:\/\//i.test(url)) return null;
  try {
    const parsed = new URL(url);
    return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? url : null;
  } catch {
    return null;
  }
}

/** Split text on URLs and wrap each in an anchor. */
export function linkify(text: string): ReactNode {
  const parts: ReactNode[] = [];
  let last = 0;
  for (const m of text.matchAll(URL_RE)) {
    const offset = m.index ?? 0;
    if (offset > last) parts.push(text.slice(last, offset));
    const href = safeHttpUrl(m[0]);
    parts.push(
      href ? (
        <a key={`${offset}:${m[0]}`} className="row-field-link" href={href} target="_blank" rel="noopener noreferrer">
          {m[0]}
        </a>
      ) : (
        m[0]
      ),
    );
    last = offset + m[0].length;
  }
  if (parts.length === 0) return text;
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}
