import assert from 'node:assert/strict';
import test from 'node:test';
import path from 'node:path';
import { executablePath, privatePythonPath, assertSupportedPlatform } from '../src/platform.mjs';

test('Windows private runtime uses executable filenames and Scripts Python', () => {
  assert.equal(executablePath('/resources', 'uv', 'win32'), path.join('/resources', 'bin', 'uv.exe'));
  assert.equal(privatePythonPath('/runtime', 'win32'), path.join('/runtime', 'Scripts', 'python.exe'));
  assert.doesNotThrow(() => assertSupportedPlatform('win32', 'x64'));
  assert.throws(() => assertSupportedPlatform('win32', 'arm64'), /Windows x64/);
});

test('existing macOS and Linux paths and platform guards remain supported', () => {
  assert.equal(executablePath('/resources', 'ffprobe', 'darwin'), path.join('/resources', 'bin', 'ffprobe'));
  assert.equal(privatePythonPath('/runtime', 'linux'), path.join('/runtime', 'bin', 'python'));
  assert.doesNotThrow(() => assertSupportedPlatform('darwin', 'arm64'));
  assert.doesNotThrow(() => assertSupportedPlatform('linux', 'x64'));
  assert.throws(() => assertSupportedPlatform('darwin', 'x64'), /Apple Silicon/);
});
