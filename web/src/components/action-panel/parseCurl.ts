export interface CurlImport {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  url: string;
  headers: { name: string; value: string }[];
  cookies: { name: string; value: string }[];
  bodyMode?: 'none' | 'form' | 'json' | 'raw';
  body?: string;
  formBody?: { name: string; value: string }[];
  contentType?: string;
  followRedirects: boolean;
}

const HTTP_METHODS = new Set(['GET', 'POST', 'PUT', 'PATCH', 'DELETE']);
const FORM_CONTENT_TYPE = 'application/x-www-form-urlencoded';

const ANSI_C_ESCAPES: Record<string, string> = {
  n: '\n',
  t: '\t',
  r: '\r',
  "'": "'",
  '\\': '\\',
};

interface TokenizeResult {
  tokens: string[];
  unterminated: boolean;
  unsafe: boolean;
}

type DataKind = 'data' | 'data-raw' | 'data-ascii' | 'data-binary' | 'data-urlencode' | 'json';

interface DataPart {
  kind: DataKind;
  value: string;
}

// These flags either affect only curl's local output, or select behavior the
// API client already provides (--fail/--basic).
const LONG_IGNORABLE_NO_ARG_FLAGS = new Set([
  '--basic',
  '--disable',
  '--fail',
  '--fail-with-body',
  '--include',
  '--no-progress-meter',
  '--progress-bar',
  '--remote-header-name',
  '--remote-name',
  '--show-error',
  '--silent',
  '--styled-output',
  '--verbose',
]);

const LONG_ARG_OUTPUT_FLAGS = new Set([
  '--cookie-jar',
  '--dump-header',
  '--output',
  '--stderr',
  '--trace',
  '--trace-ascii',
  '--trace-config',
  '--write-out',
]);

const SHORT_NO_ARG_OUTPUT_FLAGS = new Set(['#', 'J', 'N', 'O', 'S', 'f', 'i', 'q', 's', 'v']);
const SHORT_ARG_OUTPUT_FLAGS = new Set(['D', 'c', 'o', 'w']);

// Splits a shell-style command line into tokens without evaluating anything.
// Quotes and escapes are interpreted lexically; substitutions, redirects,
// pipes, config files, and @file operands are never executed or read.
function tokenize(input: string): TokenizeResult {
  const tokens: string[] = [];
  let current = '';
  let hasToken = false;
  let unterminated = false;
  let unsafe = false;
  let i = 0;
  const n = input.length;

  const flush = () => {
    if (hasToken) tokens.push(current);
    current = '';
    hasToken = false;
  };

  while (i < n) {
    const ch = input[i];

    if (ch === '\\' && input[i + 1] === '\n') {
      i += 2;
      continue;
    }
    if (ch === '\\' && input[i + 1] === '\r' && input[i + 2] === '\n') {
      i += 3;
      continue;
    }

    if (/\s/.test(ch)) {
      flush();
      i += 1;
      continue;
    }

    if (ch === "'") {
      hasToken = true;
      i += 1;
      const close = input.indexOf("'", i);
      if (close === -1) {
        current += input.slice(i);
        i = n;
        unterminated = true;
      } else {
        current += input.slice(i, close);
        i = close + 1;
      }
      continue;
    }

    if (ch === '$' && input[i + 1] === "'") {
      hasToken = true;
      i += 2;
      let closed = false;
      while (i < n) {
        if (input[i] === "'") {
          closed = true;
          break;
        }
        if (input[i] === '\\' && i + 1 < n) {
          const esc = input[i + 1];
          if (!(esc in ANSI_C_ESCAPES)) unsafe = true;
          current += esc in ANSI_C_ESCAPES ? ANSI_C_ESCAPES[esc] : esc;
          i += 2;
          continue;
        }
        current += input[i];
        i += 1;
      }
      if (closed) i += 1;
      else unterminated = true;
      continue;
    }

    if (ch === '"') {
      hasToken = true;
      i += 1;
      let closed = false;
      while (i < n) {
        if (input[i] === '"') {
          closed = true;
          break;
        }
        if (input[i] === '\\' && input[i + 1] === '\n') {
          i += 2;
          continue;
        }
        if (input[i] === '\\' && input[i + 1] === '\r' && input[i + 2] === '\n') {
          i += 3;
          continue;
        }
        if (
          input[i] === '\\'
          && ['"', '\\', '$', '`'].includes(input[i + 1])
        ) {
          current += input[i + 1];
          i += 2;
          continue;
        }
        if (input[i] === '`' || input[i] === '$') {
          unsafe = true;
        }
        current += input[i];
        i += 1;
      }
      if (closed) i += 1;
      else unterminated = true;
      continue;
    }

    if (ch === '\\' && i + 1 < n) {
      hasToken = true;
      current += input[i + 1];
      i += 2;
      continue;
    }

    // Outside quotes these characters invoke another shell command, perform
    // redirection, or trigger command/arithmetic substitution. Record and
    // reject them; never try to execute or reinterpret them.
    if ('|&;<>()'.includes(ch) || ch === '`' || ch === '$') {
      unsafe = true;
    }

    hasToken = true;
    current += ch;
    i += 1;
  }

  flush();
  return { tokens, unterminated, unsafe };
}

function removedHeaderName(token: string): string | null {
  const split = splitFirst(token, ':');
  if (!split || split[1].trim()) return null;
  const name = split[0].trim();
  return name || null;
}

function isCurlExecutable(token: string): boolean {
  const executable = token.split('/').at(-1)?.toLowerCase();
  return executable === 'curl' || executable === 'curl.exe';
}

function splitFirst(value: string, separator: string): [string, string] | null {
  const idx = value.indexOf(separator);
  if (idx === -1) return null;
  return [value.slice(0, idx), value.slice(idx + separator.length)];
}

function hasHeaderControlCharacter(value: string): boolean {
  return [...value].some((character) => {
    const code = character.charCodeAt(0);
    return code <= 0x1f || code === 0x7f;
  });
}

function parseHeaderToken(token: string): { name: string; value: string } | null {
  if (hasHeaderControlCharacter(token)) return null;
  // curl's `Name;` spelling forces an explicitly empty header value.
  if (!token.includes(':') && token.endsWith(';')) {
    const name = token.slice(0, -1).trim();
    return name ? { name, value: '' } : null;
  }
  const split = splitFirst(token, ':');
  if (!split) return null;
  const name = split[0].trim();
  // The HTTP client writes the conventional one space after ':'. Remove only
  // that delimiter space; preserve all additional leading and trailing bytes.
  const value = split[1].startsWith(' ') ? split[1].slice(1) : split[1];
  return name ? { name, value } : null;
}

function parseCookieToken(token: string): { name: string; value: string }[] | null {
  if (!token) return [];
  // With no '=', curl treats this operand as a cookie filename. The importer
  // must never read files, and pretending it is a cookie changes the request.
  if (!token.includes('=')) return null;
  const parsed = token
    .split(';')
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => {
      const split = splitFirst(part, '=');
      return split ? { name: split[0].trim(), value: split[1].trim() } : null;
    });
  if (parsed.some((part) => part === null || !part.name)) return null;
  return parsed as { name: string; value: string }[];
}

function curlFormEncode(value: string): string | null {
  try {
    return encodeURIComponent(value)
      .replace(/%20/g, '+')
      .replace(/[!'()*]/g, (character) => (
        `%${character.charCodeAt(0).toString(16).toUpperCase()}`
      ));
  } catch {
    return null;
  }
}

function dataUrlencodeValue(value: string): string | null {
  const equals = value.indexOf('=');
  const at = value.indexOf('@');
  // @file and name@file read from disk in curl. An '@' after '=' is ordinary
  // field content and is safe to encode.
  if (at >= 0 && (equals < 0 || at < equals)) return null;
  if (value.startsWith('=')) return curlFormEncode(value.slice(1));
  if (equals >= 0) {
    const encoded = curlFormEncode(value.slice(equals + 1));
    return encoded === null ? null : `${value.slice(0, equals)}=${encoded}`;
  }
  return curlFormEncode(value);
}

function renderedDataPart(part: DataPart): string | null {
  if (part.kind === 'data-urlencode') return dataUrlencodeValue(part.value);
  // --data-raw is the sole data spelling where a leading '@' is literal.
  if (part.kind !== 'data-raw' && part.value.startsWith('@')) return null;
  return part.value;
}

function appendQuery(url: string, query: string): string {
  const fragmentAt = url.indexOf('#');
  const base = fragmentAt >= 0 ? url.slice(0, fragmentAt) : url;
  const fragment = fragmentAt >= 0 ? url.slice(fragmentAt) : '';
  const separator = base.includes('?')
    ? (base.endsWith('?') || base.endsWith('&') ? '' : '&')
    : '?';
  return `${base}${separator}${query}${fragment}`;
}

function hasHeader(headers: { name: string; value: string }[], name: string): boolean {
  const normalized = name.toLowerCase();
  return headers.some((header) => header.name.toLowerCase() === normalized);
}

function lastHeader(
  headers: { name: string; value: string }[],
  name: string,
): { name: string; value: string } | undefined {
  const normalized = name.toLowerCase();
  return [...headers].reverse().find((header) => header.name.toLowerCase() === normalized);
}

function hasUnsupportedUrlGlob(url: string): boolean {
  if (url.includes('{') || url.includes('}')) return true;
  const openings = [...url.matchAll(/\[/g)].map((match) => match.index);
  const closings = [...url.matchAll(/\]/g)].map((match) => match.index);
  if (openings.length === 0 && closings.length === 0) return false;
  if (openings.length !== 1 || closings.length !== 1) return true;
  try {
    const parsed = new URL(url);
    // WHATWG URL preserves the brackets in hostname for an IPv6 literal. With
    // exactly one pair in the original text, this proves they belong to the
    // authority rather than a curl path/query range such as items[1-3].
    return !(parsed.hostname.startsWith('[') && parsed.hostname.endsWith(']'));
  } catch {
    return true;
  }
}

export function parseCurl(input: string): CurlImport | null {
  const { tokens, unterminated, unsafe } = tokenize(input.trim());
  if (unterminated || unsafe || tokens.length < 2 || !isCurlExecutable(tokens[0])) return null;

  const urls: string[] = [];
  let method: string | undefined;
  const headers: { name: string; value: string }[] = [];
  const cookies: { name: string; value: string }[] = [];
  const dataParts: DataPart[] = [];
  let followRedirects = false;
  let getMode = false;
  let jsonShortcut = false;
  let referer: string | undefined;
  let userAgent: string | undefined;
  let optionsEnded = false;
  let contentTypeRemoved = false;
  const cookieNames = new Set<string>();

  const applyHeader = (value: string): boolean => {
    if (value.startsWith('@') || hasHeaderControlCharacter(value)) return false;
    const removedName = removedHeaderName(value);
    if (removedName) {
      // The request builder can faithfully suppress curl's implicit form/JSON
      // Content-Type. Other empty-header removals (Accept:, Host:, etc.) cannot
      // be represented because the HTTP client may synthesize those defaults,
      // so fail closed instead of sending an empty header.
      if (removedName.toLowerCase() !== 'content-type') return false;
      contentTypeRemoved = true;
      for (let index = headers.length - 1; index >= 0; index -= 1) {
        if (headers[index].name.toLowerCase() === 'content-type') headers.splice(index, 1);
      }
      return true;
    }
    const parsed = parseHeaderToken(value);
    if (!parsed) return false;
    if (
      parsed.name.toLowerCase() === 'accept-encoding'
      && parsed.value.trim().toLowerCase() !== 'identity'
    ) return false;
    if (parsed.name.toLowerCase() === 'content-type') contentTypeRemoved = false;
    headers.push(parsed);
    return true;
  };

  const applyCookies = (value: string): boolean => {
    const parsed = parseCookieToken(value);
    if (!parsed) return false;
    const operandNames = new Set<string>();
    for (const cookie of parsed) {
      // Cookie names are case-sensitive. Reject only an exact repeat, which
      // the downstream dict representation would otherwise silently collapse.
      if (cookieNames.has(cookie.name) || operandNames.has(cookie.name)) return false;
      operandNames.add(cookie.name);
    }
    for (const cookie of parsed) cookieNames.add(cookie.name);
    cookies.push(...parsed);
    return true;
  };

  const takeArgument = (
    tokensIndex: number,
    inline: string | undefined,
  ): { value: string; nextIndex: number } | null => {
    if (inline !== undefined) return { value: inline, nextIndex: tokensIndex };
    if (tokensIndex + 1 >= tokens.length) return null;
    return { value: tokens[tokensIndex + 1], nextIndex: tokensIndex + 1 };
  };

  for (let i = 1; i < tokens.length; i += 1) {
    const token = tokens[i];

    if (optionsEnded || !token.startsWith('-') || token === '-') {
      urls.push(token);
      continue;
    }
    if (token === '--') {
      optionsEnded = true;
      continue;
    }

    if (token.startsWith('--')) {
      const equalsAt = token.indexOf('=');
      const flag = equalsAt >= 0 ? token.slice(0, equalsAt) : token;
      const inline = equalsAt >= 0 ? token.slice(equalsAt + 1) : undefined;

      if (flag === '--get' || flag === '--location') {
        if (inline !== undefined) return null;
        if (flag === '--get') getMode = true;
        else followRedirects = true;
        continue;
      }
      if (LONG_IGNORABLE_NO_ARG_FLAGS.has(flag)) {
        if (inline !== undefined) return null;
        continue;
      }
      if (LONG_ARG_OUTPUT_FLAGS.has(flag)) {
        const argument = takeArgument(i, inline);
        if (!argument) return null;
        i = argument.nextIndex;
        continue;
      }

      const argument = takeArgument(i, inline);
      if (!argument) return null;
      const { value } = argument;
      if (flag === '--request') method = value;
      else if (flag === '--url') urls.push(value);
      else if (flag === '--header') {
        if (!applyHeader(value)) return null;
      } else if (
        flag === '--data'
        || flag === '--data-raw'
        || flag === '--data-ascii'
        || flag === '--data-binary'
        || flag === '--data-urlencode'
      ) {
        dataParts.push({ kind: flag.slice(2) as DataKind, value });
      } else if (flag === '--json') {
        jsonShortcut = true;
        dataParts.push({ kind: 'json', value });
      } else if (flag === '--cookie') {
        if (!applyCookies(value)) return null;
      } else if (flag === '--referer') referer = value;
      else if (flag === '--user-agent') userAgent = value;
      else {
        // Unknown flags may change the method, body, authentication, proxy,
        // TLS, URL, or headers. Reject rather than guessing how many operands
        // they consume and silently importing a different request. This also
        // rejects --form, --config, --upload-file, --insecure, and friends —
        // and --user, whose credentials would otherwise be base64-encoded
        // into a persisted Authorization header outside the app's sanctioned
        // {{secret.NAME}} path.
        return null;
      }
      i = argument.nextIndex;
      continue;
    }

    // Short options can be clustered (-fsSL) and argument-taking options can
    // carry an attached value (-XPOST, -H'Accept: ...', -dname=value).
    for (let position = 1; position < token.length; position += 1) {
      const flag = token[position];
      if (flag === 'G' || flag === 'L') {
        if (flag === 'G') getMode = true;
        else followRedirects = true;
        continue;
      }
      if (SHORT_NO_ARG_OUTPUT_FLAGS.has(flag)) continue;

      const attached = token.slice(position + 1);
      if (SHORT_ARG_OUTPUT_FLAGS.has(flag)) {
        const argument = takeArgument(i, attached || undefined);
        if (!argument) return null;
        i = argument.nextIndex;
        break;
      }

      if (!['A', 'H', 'X', 'b', 'd', 'e'].includes(flag)) {
        // Includes request-changing or file-reading flags such as -F, -K, -T,
        // -I, -k, -m, and -x — and the credential-bearing -u, which is
        // rejected rather than persisted as a Basic Authorization header.
        return null;
      }
      const argument = takeArgument(i, attached || undefined);
      if (!argument) return null;
      const { value } = argument;
      if (flag === 'X') method = value;
      else if (flag === 'H') {
        if (!applyHeader(value)) return null;
      } else if (flag === 'd') dataParts.push({ kind: 'data', value });
      else if (flag === 'b') {
        if (!applyCookies(value)) return null;
      } else if (flag === 'e') referer = value;
      else userAgent = value;
      i = argument.nextIndex;
      break;
    }
  }

  if (
    urls.length !== 1
    || !/^https?:\/\//i.test(urls[0])
    // curl expands braces/ranges into multiple transfers unless --globoff is
    // used; neither behavior maps to one API action, so reject both globs and
    // --globoff rather than silently sending one literal URL.
    || hasUnsupportedUrlGlob(urls[0])
  ) return null;

  let data: string | undefined;
  for (const part of dataParts) {
    const rendered = renderedDataPart(part);
    if (rendered === null) return null;
    // curl's --json shortcut concatenates its next piece directly, while the
    // ordinary --data family places '&' before each subsequent piece.
    data = data === undefined ? rendered : `${data}${part.kind === 'json' ? '' : '&'}${rendered}`;
  }

  let url = urls[0];
  if (getMode && data !== undefined) url = appendQuery(url, data);
  const hasBody = !getMode && data !== undefined;

  const resolvedMethod = (method?.trim().toUpperCase() || (hasBody ? 'POST' : 'GET'));
  if (!HTTP_METHODS.has(resolvedMethod)) return null;
  // The current API-call form deliberately has no GET body mode. Reject a cURL
  // GET-with-body rather than successfully importing and then dropping it.
  if (resolvedMethod === 'GET' && hasBody) return null;

  if (referer !== undefined && !hasHeader(headers, 'Referer')) {
    headers.push({ name: 'Referer', value: referer });
  }
  if (userAgent !== undefined && !hasHeader(headers, 'User-Agent')) {
    headers.push({ name: 'User-Agent', value: userAgent });
  }
  if (jsonShortcut) {
    if (!contentTypeRemoved && !hasHeader(headers, 'Content-Type')) {
      headers.push({ name: 'Content-Type', value: 'application/json' });
    }
    if (!hasHeader(headers, 'Accept')) {
      headers.push({ name: 'Accept', value: 'application/json' });
    }
  }

  let bodyMode: CurlImport['bodyMode'] = 'none';
  let body: string | undefined;
  let contentType: string | undefined;
  if (hasBody && data !== undefined) {
    body = data;
    const contentTypeHeader = lastHeader(headers, 'Content-Type');
    const effectiveContentType = contentTypeHeader?.value
      ?? (contentTypeRemoved ? '' : FORM_CONTENT_TYPE);
    // Always use raw mode, including for valid JSON. JSON mode parses and
    // re-serializes, which changes whitespace, duplicate keys, number spelling,
    // and signed payload bytes. Raw mode is the faithful cURL representation.
    bodyMode = 'raw';
    contentType = effectiveContentType || undefined;
  }

  return {
    method: resolvedMethod as CurlImport['method'],
    url,
    headers,
    cookies,
    bodyMode,
    body,
    contentType,
    followRedirects,
  };
}
