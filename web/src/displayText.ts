const ESCAPED_UNICODE_RE = /\\u([0-9a-fA-F]{4})/g;

export interface TextDisplayOptions {
  decodeEscapes?: boolean;
}

export function decodeEscapedText(value: string): string {
  return value
    .replace(ESCAPED_UNICODE_RE, (_match, hex: string) =>
      String.fromCharCode(Number.parseInt(hex, 16)),
    )
    .replace(/\\r\\n/g, '\n')
    .replace(/\\n/g, '\n')
    .replace(/\\r/g, '\r')
    .replace(/\\t/g, '\t');
}

export function looksLikeHtml(value: string): boolean {
  return /<\/?[a-z][\s\S]*>/i.test(value);
}

export function textDisplayValue(value: unknown, options: TextDisplayOptions = {}): string {
  if (value == null) return '';
  const raw = String(value);
  return options.decodeEscapes ? decodeEscapedText(raw) : raw;
}
