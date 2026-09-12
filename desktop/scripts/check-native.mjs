import { execFile as execFileCallback } from 'node:child_process';
import { promisify } from 'node:util';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { prepareRuntime } from '../src/provision.mjs';

const execFile = promisify(execFileCallback);
const READY_MARKER = '.frisket-runtime-ready.json';

function requiredAbsolute(name) {
  const value = process.env[name];
  if (!value || !path.isAbsolute(value)) {
    throw new Error(`${name} must name an absolute installed-app path.`);
  }
  return value;
}

async function hasCompletedRuntime(dataPath) {
  let entries;
  try {
    entries = await fs.readdir(path.join(dataPath, 'runtimes'), { withFileTypes: true });
  } catch {
    return false;
  }
  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    try {
      await fs.access(path.join(dataPath, 'runtimes', entry.name, READY_MARKER));
      return true;
    } catch {
      // An incomplete environment must not be mistaken for the UI-created one.
    }
  }
  return false;
}

async function run(label, command, args, options) {
  try {
    return await execFile(command, args, { ...options, maxBuffer: 1024 * 1024, timeout: 120_000 });
  } catch (error) {
    const code = error.code ? ` (${error.code})` : '';
    throw new Error(`${label} failed${code}. ${String(error.stderr || error.message).slice(-4096)}`);
  }
}

function parseJson(label, value) {
  try {
    return JSON.parse(value);
  } catch {
    throw new Error(`${label} did not produce JSON.`);
  }
}

async function checkMedia(resourcesPath, env, workspace) {
  const bin = path.join(resourcesPath, 'bin');
  const movie = path.join(workspace, 'native-check.mp4');
  await run('Bundled ffmpeg libx264/AAC transcode', path.join(bin, 'ffmpeg'), [
    '-hide_banner', '-loglevel', 'error',
    '-f', 'lavfi', '-i', 'color=c=black:s=64x64:d=0.2',
    '-f', 'lavfi', '-i', 'sine=frequency=1000:duration=0.2',
    '-shortest', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
    '-movflags', '+faststart', movie,
  ], { env });
  const probe = await run('Bundled ffprobe stream inspection', path.join(bin, 'ffprobe'), [
    '-v', 'error', '-show_entries', 'stream=codec_type,codec_name', '-of', 'json', movie,
  ], { env });
  const streams = parseJson('Bundled ffprobe', probe.stdout).streams;
  if (!Array.isArray(streams)
    || !streams.some((stream) => stream.codec_type === 'video' && stream.codec_name === 'h264')
    || !streams.some((stream) => stream.codec_type === 'audio' && stream.codec_name === 'aac')) {
    throw new Error('Bundled ffmpeg output is missing h264 video or AAC audio.');
  }
  await run('Bundled Deno evaluation', path.join(bin, 'deno'), [
    'eval', 'if (!Deno.version.deno) Deno.exit(1);',
  ], { env });
}

async function checkPython(resourcesPath, python, env, workspace) {
  const script = path.join(workspace, 'native-python-check.py');
  const pdf = path.join(workspace, 'native-check.pdf');
  await fs.writeFile(script, `
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
workspace = Path(sys.argv[2])

from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfWriter
from frisket.ops.ocr_engines_local import ocr_rapidocr, rapidocr_execution_scope
from frisket.engine.pdf_render import render_pdf_pages

pdf = workspace / "native-check.pdf"
writer = PdfWriter()
writer.add_blank_page(width=144, height=144)
with pdf.open("wb") as target:
    writer.write(target)

image = Image.new("RGB", (1280, 360), "white")
draw = ImageDraw.Draw(image)
try:
    font = ImageFont.load_default(size=64)
except TypeError:
    font = ImageFont.load_default()
draw.text((80, 120), "FRISKET 4271", fill="black", font=font)
image_path = workspace / "rapidocr.png"
image.save(image_path)

async def main():
    render_dir = workspace / "rendered"
    render_dir.mkdir()
    rendered = await render_pdf_pages(pdf, render_dir, dpi=144)
    if rendered.page_count != 1 or len(rendered.pages) != 1:
        raise RuntimeError("PDFium did not render the generated PDF")
    with Image.open(rendered.pages[0][1]) as page_image:
        if page_image.size != (288, 288):
            raise RuntimeError("PDFium rendered an unexpected page size")
    async with rapidocr_execution_scope(expected_rows=1, language=None) as pool:
        pages = await ocr_rapidocr(pool, [image_path], workspace)
    text = " ".join(str(page.get("text", "")) for page in pages)
    normalized = "".join(character for character in text.upper() if character.isalnum())
    if "FRISKET" not in normalized or "4271" not in normalized:
        raise RuntimeError("RapidOCR did not recognize the generated sentinel")
    print(json.dumps({"pages": len(pages), "text": text}))

asyncio.run(main())
`, { mode: 0o600 });
  const resourcesPython = path.join(resourcesPath, 'python');
  const ocr = await run('Private Python fenced RapidOCR', python, ['-I', script, resourcesPython, workspace], { env });
  const ocrResult = parseJson('Private Python fenced RapidOCR', ocr.stdout);
  if (ocrResult.pages !== 1 || typeof ocrResult.text !== 'string') {
    throw new Error('Private Python fenced RapidOCR returned an invalid result.');
  }
  const bootstrap = path.join(resourcesPython, 'frisket', 'runtime', '_bootstrap.py');
  const pypdf = await run('Private Python bootstrap PDF parser', python, ['-I', bootstrap, 'pypdf', pdf], { env });
  const pdfResult = parseJson('Private Python bootstrap PDF parser', pypdf.stdout);
  if (pdfResult.page_count !== 1 || pdfResult.parse_error || pdfResult.dependency_missing) {
    throw new Error('Private Python bootstrap PDF parser could not read its generated PDF.');
  }
}

async function main() {
  if (process.platform !== 'darwin' || process.arch !== 'arm64') {
    throw new Error('Native runtime proof requires macOS Apple Silicon.');
  }
  const appPath = requiredAbsolute('FRISKET_DESKTOP_APP');
  const dataPath = requiredAbsolute('FRISKET_DESKTOP_PROFILE');
  const resourcesPath = path.join(appPath, 'Contents', 'Resources');
  if (!await hasCompletedRuntime(dataPath)) {
    throw new Error('Installed UI journey did not produce a completed private runtime.');
  }
  const runtime = await prepareRuntime({ resourcesPath, dataPath });
  const workspace = await fs.mkdtemp(path.join(os.tmpdir(), 'frisket-native-'));
  try {
    await checkMedia(resourcesPath, runtime.env, workspace);
    await checkPython(resourcesPath, runtime.python, runtime.env, workspace);
  } finally {
    await fs.rm(workspace, { recursive: true, force: true });
  }
}

await main();
