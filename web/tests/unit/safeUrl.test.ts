import { GridCellKind } from '@glideapps/glide-data-grid';
import { describe, expect, it } from 'vitest';

import { safeHttpUrl, safeMediaUrl } from '../../src/core/safeUrl';
import { buildCell } from '../../src/grid/cells';
import { resolveMediaValue } from '../../src/media/resolveMediaValue';
import { columnDef, row } from '../support/domainFixtures';

const SCRIPT_URLS = [
  'javascript:alert(1)',
  'JaVaScRiPt:alert(1)',
  ' \tjavascript:alert(1)',
  '\u0001javascript:alert(1)',
  'java\tscript:alert(1)',
  'vbscript:msgbox(1)',
  'data:text/html,<script>alert(1)</script>',
  'data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==',
];

describe('safeHttpUrl', () => {
  it.each([
    ['https://example.test/a?b=1#c', 'https://example.test/a?b=1#c'],
    ['http://example.test', 'http://example.test'],
    ['HTTPS://Example.test/Path', 'HTTPS://Example.test/Path'],
    ['  https://example.test/padded \n', 'https://example.test/padded'],
  ])('keeps the http(s) URL %j', (raw, expected) => {
    expect(safeHttpUrl(raw)).toBe(expected);
  });

  it.each([
    ...SCRIPT_URLS,
    'data:image/png;base64,AAAA',
    '//evil.example/path',
    '/api/projects/p/blobs/abc',
    'relative/path.html',
    'mailto:someone@example.test',
    'file:///etc/passwd',
    'https://',
    'http://[::1',
    '\u0001https://example.test',
    '',
  ])('refuses %j', (raw) => {
    expect(safeHttpUrl(raw)).toBeNull();
  });

  it('refuses absent values', () => {
    expect(safeHttpUrl(null)).toBeNull();
    expect(safeHttpUrl(undefined)).toBeNull();
  });
});

describe('safeMediaUrl', () => {
  it.each([
    ['https://example.test/photo.png', 'https://example.test/photo.png'],
    ['data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=', 'data:image/svg+xml;base64,PHN2Zz48L3N2Zz4='],
    ['DATA:audio/mpeg;base64,AAAA', 'DATA:audio/mpeg;base64,AAAA'],
    ['data:video/mp4,AAAA', 'data:video/mp4,AAAA'],
    [
      '/api/projects/p/actions/v1/preview/s/artifacts/abc123',
      '/api/projects/p/actions/v1/preview/s/artifacts/abc123',
    ],
    ['/api/projects/p/x/../blobs/abc?download=1', '/api/projects/p/blobs/abc?download=1'],
  ])('keeps the media URL %j', (raw, expected) => {
    expect(safeMediaUrl(raw)).toBe(expected);
  });

  it.each([
    ...SCRIPT_URLS,
    'data:application/pdf;base64,AAAA',
    'data:image/',
    '//evil.example/photo.png',
    '/\\evil.example/photo.png',
    '/api/../outside',
    '/Users/me/Desktop/photo.png',
    'photo.png',
    'blob:https://example.test/0d2c',
  ])('refuses %j', (raw) => {
    expect(safeMediaUrl(raw)).toBeNull();
  });
});

describe('resolveMediaValue', () => {
  it('scopes a blob envelope to the project blob route', () => {
    const blob = 'a'.repeat(64);
    expect(resolveMediaValue(JSON.stringify({ blob, filename: 'scan.png', mime: 'image/png' }), 'p1')).toEqual({
      url: `/api/projects/p1/blobs/${blob}`, label: 'scan.png', filename: 'scan.png', mime: 'image/png', blobHash: blob,
    });
  });

  it('keeps a plain http(s) URL and its filename label', () => {
    expect(resolveMediaValue('https://example.test/media/clip.mp4', 'p1')).toEqual({
      url: 'https://example.test/media/clip.mp4', label: 'clip.mp4',
    });
  });

  it.each([...SCRIPT_URLS, '/Users/me/clip.mp4', '{"not": "an envelope"}'])(
    'resolves no media for %j', (raw) => {
      expect(resolveMediaValue(raw, 'p1')).toBeNull();
    },
  );
});

describe('grid media cells', () => {
  it.each(['image', 'file', 'video', 'audio'])(
    'shows a non-media %s value as text instead of a media cell', (type) => {
      const col = columnDef({ id: 'm', name: 'media', type });
      const cell = buildCell(col, row({ m: 'javascript:alert(1)' }), { projectId: 'p1' });
      expect(cell.kind).toBe(GridCellKind.Text);
      expect((cell as { displayData?: string }).displayData).toContain('javascript:alert(1)');
    },
  );

  it('keeps an https image as an image cell', () => {
    const col = columnDef({ id: 'm', name: 'media', type: 'image' });
    const cell = buildCell(col, row({ m: 'https://example.test/photo.png' }), { projectId: 'p1' });
    if (cell.kind !== GridCellKind.Image) {
      throw new Error(`Expected image cell, got ${cell.kind}`);
    }
    expect(cell.data).toEqual(['https://example.test/photo.png']);
  });
});
