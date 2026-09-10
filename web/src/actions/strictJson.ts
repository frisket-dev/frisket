/** Raised when an otherwise-valid JSON object spells the same decoded key twice. */
export class DuplicateJsonKeyError extends SyntaxError {
  readonly key: string;

  constructor(key: string) {
    super(`Duplicate JSON key: ${JSON.stringify(key)}.`);
    this.name = 'DuplicateJsonKeyError';
    this.key = key;
  }
}

/**
 * Walk the original JSON text because JSON.parse's reviver runs only after
 * duplicate object members have already collapsed to the last value. The
 * input is parsed normally first, so this scanner only needs to preserve JSON
 * structure and decode member names; JSON.parse remains the syntax authority.
 */
class JsonObjectKeyScanner {
  private index = 0;
  private readonly source: string;

  constructor(source: string) {
    this.source = source;
  }

  scan(): void {
    this.scanValue();
    this.skipWhitespace();
    if (this.index !== this.source.length) {
      throw new SyntaxError('Unexpected trailing JSON content.');
    }
  }

  private scanValue(): void {
    this.skipWhitespace();
    const token = this.source[this.index];
    if (token === '{') {
      this.scanObject();
      return;
    }
    if (token === '[') {
      this.scanArray();
      return;
    }
    if (token === '"') {
      this.scanString();
      return;
    }
    this.scanPrimitive();
  }

  private scanObject(): void {
    this.index += 1;
    this.skipWhitespace();
    if (this.consume('}')) return;

    const keys = new Set<string>();
    while (this.index < this.source.length) {
      this.skipWhitespace();
      const key = this.scanString();
      if (keys.has(key)) throw new DuplicateJsonKeyError(key);
      keys.add(key);

      this.skipWhitespace();
      this.expect(':');
      this.scanValue();
      this.skipWhitespace();
      if (this.consume('}')) return;
      this.expect(',');
    }
    throw new SyntaxError('Unterminated JSON object.');
  }

  private scanArray(): void {
    this.index += 1;
    this.skipWhitespace();
    if (this.consume(']')) return;

    while (this.index < this.source.length) {
      this.scanValue();
      this.skipWhitespace();
      if (this.consume(']')) return;
      this.expect(',');
    }
    throw new SyntaxError('Unterminated JSON array.');
  }

  private scanString(): string {
    if (this.source[this.index] !== '"') {
      throw new SyntaxError('Expected a JSON string.');
    }
    const start = this.index;
    this.index += 1;
    while (this.index < this.source.length) {
      const token = this.source[this.index];
      this.index += 1;
      if (token === '"') {
        return JSON.parse(this.source.slice(start, this.index)) as string;
      }
      if (token === '\\') this.index += 1;
    }
    throw new SyntaxError('Unterminated JSON string.');
  }

  private scanPrimitive(): void {
    const start = this.index;
    while (this.index < this.source.length) {
      const token = this.source[this.index];
      if (token === ',' || token === ']' || token === '}' || /\s/.test(token)) break;
      this.index += 1;
    }
    if (this.index === start) throw new SyntaxError('Expected a JSON value.');
  }

  private skipWhitespace(): void {
    while (/\s/.test(this.source[this.index] ?? '')) this.index += 1;
  }

  private consume(token: string): boolean {
    if (this.source[this.index] !== token) return false;
    this.index += 1;
    return true;
  }

  private expect(token: string): void {
    if (!this.consume(token)) throw new SyntaxError(`Expected ${JSON.stringify(token)}.`);
  }
}

/** Parse JSON without accepting duplicate keys or values that overflow to infinity. */
export function parseStrictJson(source: string): unknown {
  const parsed: unknown = JSON.parse(source, (_key, value: unknown) => {
    if (typeof value === 'number' && !Number.isFinite(value)) {
      throw new SyntaxError('JSON numbers must be finite.');
    }
    return value;
  });
  new JsonObjectKeyScanner(source).scan();
  return parsed;
}
