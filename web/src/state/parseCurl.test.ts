import { describe, expect, it } from 'vitest';
import { parseCurl } from '../components/action-panel/parseCurl';

describe('parseCurl', () => {
  it('parses a bare GET with a query string kept in the URL', () => {
    const result = parseCurl(`curl 'https://x/a?b=1'`);
    expect(result).not.toBeNull();
    expect(result?.method).toBe('GET');
    expect(result?.url).toBe('https://x/a?b=1');
  });

  it('parses browser-style JSON cURL with explicit headers', () => {
    const result = parseCurl(
      `curl -X POST 'https://api.example.com/items' -H 'Content-Type: application/json' -H 'Accept: application/json' --data-raw '{"k":"v"}'`,
    );
    expect(result?.method).toBe('POST');
    expect(result?.headers).toEqual([
      { name: 'Content-Type', value: 'application/json' },
      { name: 'Accept', value: 'application/json' },
    ]);
    expect(result?.bodyMode).toBe('raw');
    expect(result?.body).toBe('{"k":"v"}');
    expect(result?.contentType).toBe('application/json');
  });

  it('preserves ordinary -d bytes and curl\'s implicit form content type', () => {
    const result = parseCurl(`curl 'https://api.example.com/items' --data 'a=hello+world&odd'`);
    expect(result).toMatchObject({
      method: 'POST',
      bodyMode: 'raw',
      body: 'a=hello+world&odd',
      contentType: 'application/x-www-form-urlencoded',
    });
    expect(result?.headers).toEqual([]);
  });

  it('does not guess that JSON-looking -d data has application/json semantics', () => {
    const result = parseCurl(`curl 'https://x/a' --data '{"a":1}'`);
    expect(result).toMatchObject({
      method: 'POST',
      bodyMode: 'raw',
      body: '{"a":1}',
      contentType: 'application/x-www-form-urlencoded',
    });
  });

  it('preserves an explicit non-JSON content type with a raw body', () => {
    const result = parseCurl(
      `curl https://x/a -H 'Content-Type: application/x-www-form-urlencoded' -d 'a=1&&b=two+words'`,
    );
    expect(result).toMatchObject({
      bodyMode: 'raw',
      body: 'a=1&&b=two+words',
      contentType: 'application/x-www-form-urlencoded',
    });
  });

  it('implements --json as data plus default JSON headers', () => {
    const result = parseCurl(`curl --json '{"a":1}' https://x/a`);
    expect(result).toMatchObject({
      method: 'POST',
      bodyMode: 'raw',
      body: '{"a":1}',
      contentType: 'application/json',
    });
    expect(result?.headers).toEqual([
      { name: 'Content-Type', value: 'application/json' },
      { name: 'Accept', value: 'application/json' },
    ]);
  });

  it('--json does not duplicate explicit content-type or accept headers', () => {
    const result = parseCurl(
      `curl --json '{"a":1}' -H 'Content-Type: application/problem+json' -H 'Accept: text/json' https://x/a`,
    );
    expect(result?.headers).toEqual([
      { name: 'Content-Type', value: 'application/problem+json' },
      { name: 'Accept', value: 'text/json' },
    ]);
    expect(result?.bodyMode).toBe('raw');
    expect(result?.contentType).toBe('application/problem+json');
  });

  it('uses curl\'s direct concatenation for repeated --json pieces', () => {
    const result = parseCurl(`curl --json '{"a":' --json '1}' https://x/a`);
    expect(result).toMatchObject({
      bodyMode: 'raw',
      body: '{"a":1}',
      contentType: 'application/json',
    });
  });

  it('uses the spelling of each later data option to choose its separator', () => {
    expect(parseCurl(`curl --json A --data B https://x/a`)?.body).toBe('A&B');
    expect(parseCurl(`curl --data A --json B https://x/a`)?.body).toBe('AB');
  });

  it('keeps invalid JSON bytes raw rather than repairing them', () => {
    const result = parseCurl(`curl --json '{nope}' https://x/a`);
    expect(result).toMatchObject({
      bodyMode: 'raw',
      body: '{nope}',
      contentType: 'application/json',
    });
  });

  it('parses -b cookies split on semicolons', () => {
    const result = parseCurl(`curl 'https://x/a' -b 'x=1; y=2'`);
    expect(result?.cookies).toEqual([
      { name: 'x', value: '1' },
      { name: 'y', value: '2' },
    ]);
  });

  it('rejects duplicate cookie names that the request dictionary would collapse', () => {
    expect(parseCurl(`curl 'https://x/a' -b 'session=tenantA; session=tenantB'`)).toBeNull();
    expect(parseCurl(
      `curl 'https://x/a' -b 'session=tenantA' --cookie 'session=tenantB'`,
    )).toBeNull();
  });

  it('keeps distinct case-sensitive cookie names', () => {
    expect(parseCurl(`curl 'https://x/a' -b 'session=lower; Session=upper'`)?.cookies).toEqual([
      { name: 'session', value: 'lower' },
      { name: 'Session', value: 'upper' },
    ]);
  });

  it('rejects the credential-bearing -u/--user flag rather than persisting Basic auth', () => {
    for (const command of [
      `curl -u user:pass https://x/a`,
      `curl -uuser:pass https://x/a`,
      `curl -u 'usér:päss' https://x/a`,
      `curl --user user:pass https://x/a`,
      `curl --user=user:pass https://x/a`,
      `curl --user=user https://x/a`,
      `curl -u user: https://x/a`,
      `curl -u user https://x/a`,
    ]) {
      expect(parseCurl(command), command).toBeNull();
    }
  });

  it('tokenizes single quotes, $\'...\' ANSI-C body quoting, and continuations', () => {
    const result = parseCurl(
      `curl 'https://x/a' \\\n  -H 'X-Note: ok' \\\n  --data-raw $'line1\\nline2'`,
    );
    expect(result?.url).toBe('https://x/a');
    expect(result?.headers).toEqual([{ name: 'X-Note', value: 'ok' }]);
    expect(result?.body).toBe('line1\nline2');
  });

  it('rejects ANSI-C escapes the lexical subset cannot reproduce', () => {
    expect(parseCurl(`curl --data-raw $'\\x41' https://x/a`)).toBeNull();
  });

  it('rejects control characters in imported header names or values', () => {
    expect(parseCurl(`curl -H $'X-Note: line1\\nInjected: yes' https://x/a`)).toBeNull();
    expect(parseCurl(`curl -H $'X-Note: value\\tmore' https://x/a`)).toBeNull();
    expect(parseCurl(`curl -H $'Content-Type:\\n' -d x https://x/a`)).toBeNull();
  });

  it('supports long --flag=value and attached short option values', () => {
    const long = parseCurl(
      `curl --url=https://x/a --request=PATCH --header='Content-Type: application/json' --data-raw='{"x":1}'`,
    );
    expect(long).toMatchObject({ method: 'PATCH', url: 'https://x/a', bodyMode: 'raw' });

    const short = parseCurl(
      `curl -sSL -XPOST -H'Content-Type: application/json' -d'{"x":1}' https://x/a`,
    );
    expect(short).toMatchObject({
      method: 'POST',
      followRedirects: true,
      bodyMode: 'raw',
      body: '{"x":1}',
    });
  });

  it('ignores local output flags without mistaking their operands for URLs', () => {
    const result = parseCurl(
      `curl --output resp.txt --write-out '%{http_code}' -D headers.txt https://example.com/api`,
    );
    expect(result?.url).toBe('https://example.com/api');
    expect(result?.method).toBe('GET');
  });

  it('sets followRedirects when -L is present', () => {
    expect(parseCurl(`curl -L 'https://x/a'`)?.followRedirects).toBe(true);
    expect(parseCurl(`curl 'https://x/a'`)?.followRedirects).toBe(false);
  });

  it('rejects --compressed because safe_request forces identity encoding', () => {
    expect(parseCurl(`curl --compressed https://x/a`)).toBeNull();
  });

  it('accepts only the identity Accept-Encoding enforced by the transport', () => {
    expect(parseCurl(`curl -H 'Accept-Encoding: identity' https://x/a`)).not.toBeNull();
    expect(parseCurl(`curl -H 'Accept-Encoding: gzip' https://x/a`)).toBeNull();
    expect(parseCurl(`curl -H 'Accept-Encoding;' https://x/a`)).toBeNull();
  });

  it('faithfully suppresses curl\'s implicit Content-Type on an empty override', () => {
    const result = parseCurl(`curl -d 'a=1' -H 'Content-Type:' https://x/a`);
    expect(result).toMatchObject({
      bodyMode: 'raw',
      body: 'a=1',
    });
    expect(result?.headers).not.toContainEqual(expect.objectContaining({ name: 'Content-Type' }));
    expect(result?.contentType).toBeUndefined();
    // Other client-generated headers cannot be suppressed by CurlImport, so
    // those commands fail closed rather than sending an empty replacement.
    expect(parseCurl(`curl -H 'Accept:' https://x/a`)).toBeNull();
  });

  it('correctly URL-encodes --data-urlencode in -G mode', () => {
    const result = parseCurl(
      `curl -G 'https://example.com/s?fixed=1#frag' --data-urlencode "q=a b&c!'()*~"`,
    );
    expect(result?.method).toBe('GET');
    expect(result?.url).toBe(
      'https://example.com/s?fixed=1&q=a+b%26c%21%27%28%29%2A~#frag',
    );
    expect(result?.bodyMode).toBe('none');
    expect(result?.body).toBeUndefined();
  });

  it('supports every non-file --data-urlencode spelling', () => {
    expect(parseCurl(
      `curl -G https://x/a --data-urlencode 'plain value' --data-urlencode '=bare value' --data-urlencode 'name=a&b'`,
    )?.url).toBe('https://x/a?plain+value&bare+value&name=a%26b');
  });

  it('uses encoded data and form content type for body-mode --data-urlencode', () => {
    const result = parseCurl(`curl https://x/a --data-urlencode 'q=a b&c'`);
    expect(result).toMatchObject({
      method: 'POST',
      bodyMode: 'raw',
      body: 'q=a+b%26c',
      contentType: 'application/x-www-form-urlencoded',
    });
  });

  it('keeps --data-raw @ literal but rejects data options that read @files', () => {
    expect(parseCurl(`curl https://x/a --data-raw '@literal'`)).toMatchObject({
      body: '@literal',
    });
    for (const command of [
      `curl https://x/a --data @payload.txt`,
      `curl https://x/a --data-binary=@payload.bin`,
      `curl https://x/a --json @payload.json`,
      `curl https://x/a --data-urlencode @payload.txt`,
      `curl https://x/a --data-urlencode field@payload.txt`,
    ]) {
      expect(parseCurl(command), command).toBeNull();
    }
  });

  it('rejects config, upload, header-file, and cookie-file operands', () => {
    for (const command of [
      `curl --config=/tmp/curlrc https://x/a`,
      `curl -K/tmp/curlrc https://x/a`,
      `curl -Tpayload.bin https://x/a`,
      `curl --upload-file payload.bin https://x/a`,
      `curl -H @headers.txt https://x/a`,
      `curl -b cookies.txt https://x/a`,
    ]) {
      expect(parseCurl(command), command).toBeNull();
    }
  });

  it('rejects multipart and other unsupported request-changing flags', () => {
    for (const command of [
      `curl -F 'x=y' https://x/a`,
      `curl --form=x=y https://x/a`,
      `curl --form-string 'x=y' https://x/a`,
      `curl --max-time 10 https://x/a`,
      `curl -m10 https://x/a`,
      `curl --proxy http://proxy.test https://x/a`,
      `curl -k https://x/a`,
      `curl --globoff 'https://x/{one,two}'`,
      `curl -g 'https://x/{one,two}'`,
      `curl --frobnicate widget https://x/a`,
    ]) {
      expect(parseCurl(command), command).toBeNull();
    }
  });

  it('rejects methods the API action cannot represent and GET bodies it would drop', () => {
    expect(parseCurl(`curl -X OPTIONS https://x/a`)).toBeNull();
    expect(parseCurl(`curl -I https://x/a`)).toBeNull();
    expect(parseCurl(`curl -X GET -d 'a=1' https://x/a`)).toBeNull();
  });

  it('rejects malformed or ambiguous command shapes', () => {
    for (const command of [
      `curl -X POST -H 'Content-Type: application/json'`,
      `curl 'https://example.com/api`,
      `wget https://example.com/api`,
      `curl https://one.example https://two.example`,
      `curl ftp://example.com/file`,
      `curl 'https://x/{one,two}'`,
      `curl 'https://x/items[1-3]'`,
      `curl --url`,
    ]) {
      expect(parseCurl(command), command).toBeNull();
    }
  });

  it('allows brackets only for a syntactically bracketed IPv6 authority', () => {
    expect(parseCurl(`curl 'https://[2001:db8::1]:8443/items'`)).toMatchObject({
      url: 'https://[2001:db8::1]:8443/items',
    });
    expect(parseCurl(`curl 'https://example.com/items[1]'`)).toBeNull();
    expect(parseCurl(`curl 'https://[2001:db8::1]/items[1]'`)).toBeNull();
  });

  it('is inert: shell-looking data remains ordinary body text', () => {
    const result = parseCurl(
      `curl --data-raw '$(touch /tmp/never-run); ` + '`id`' + `' https://x/a`,
    );
    expect(result?.body).toBe('$(touch /tmp/never-run); `id`');
  });

  it('rejects executable shell syntax outside literal quotes', () => {
    for (const command of [
      `curl https://x/a | sh`,
      `curl https://x/a>response.txt`,
      `curl https://x/a < request.txt`,
      `curl https://x/a; touch /tmp/never-run`,
      `curl --data-raw $(id) https://x/a`,
      'curl --data-raw `id` https://x/a',
      `curl --data-raw "$(id)" https://x/a`,
      `curl --data-raw $TOKEN https://x/a`,
      `curl --data-raw "${'$'}TOKEN" https://x/a`,
    ]) {
      expect(parseCurl(command), command).toBeNull();
    }
  });

  it('preserves header whitespace after the conventional delimiter space', () => {
    expect(parseCurl(`curl -H 'X-Signature:  padded  ' https://x/a`)?.headers).toEqual([
      { name: 'X-Signature', value: ' padded  ' },
    ]);
  });

  it('lets explicit user-agent and referer headers override shorthand flags', () => {
    const result = parseCurl(
      `curl -A generated-agent -e https://generated.test `
      + `-H 'Authorization: Bearer explicit' -H 'User-Agent: explicit-agent' `
      + `-H 'Referer: https://explicit.test' https://x/a`,
    );
    expect(result?.headers).toEqual([
      { name: 'Authorization', value: 'Bearer explicit' },
      { name: 'User-Agent', value: 'explicit-agent' },
      { name: 'Referer', value: 'https://explicit.test' },
    ]);
  });
});
