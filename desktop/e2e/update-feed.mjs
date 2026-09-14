import { createServer } from 'node:http';
import { createReadStream } from 'node:fs';
import { readFile, stat } from 'node:fs/promises';
import path from 'node:path';

// The release workflow writes JSON, which is also valid update-feed YAML.
// Keep the release's digest and size; change only the delivery address.
export function localMetadata(metadata, origin) {
  const local = structuredClone(metadata);
  const artifact = (value) => `${origin}/${encodeURIComponent(decodeURIComponent(path.posix.basename(new URL(value, `${origin}/`).pathname)))}`;
  local.files = local.files.map((file) => ({ ...file, url: artifact(file.url) }));
  if (local.path) local.path = artifact(local.path);
  return local;
}

export async function serveUpdateFeed(directory, platform = process.platform) {
  const metadataName = platform === 'darwin' ? 'latest-mac.yml' : 'latest.yml';
  const metadata = JSON.parse(await readFile(path.join(directory, metadataName), 'utf8'));
  const files = new Map();
  for (const file of metadata.files) {
    const name = decodeURIComponent(path.posix.basename(new URL(file.url, 'https://release.invalid/').pathname));
    files.set(name, path.join(directory, name));
    await stat(files.get(name));
  }
  const requests = [];
  let origin;
  const server = createServer(async (request, response) => {
    const name = decodeURIComponent(new URL(request.url, origin).pathname.slice(1));
    requests.push({ name, method: request.method });
    try {
      if (!['GET', 'HEAD'].includes(request.method)) {
        response.writeHead(405).end();
      } else if (name === metadataName) {
        const body = JSON.stringify(localMetadata(metadata, origin));
        response.writeHead(200, { 'Content-Type': 'application/yaml', 'Content-Length': Buffer.byteLength(body) });
        response.end(request.method === 'HEAD' ? undefined : body);
      } else if (files.has(name)) {
        const filename = files.get(name);
        const info = await stat(filename);
        response.writeHead(200, { 'Content-Type': 'application/octet-stream', 'Content-Length': info.size });
        if (request.method === 'HEAD') response.end();
        else createReadStream(filename).on('error', () => response.destroy()).pipe(response);
      } else response.writeHead(404).end();
    } catch { response.writeHead(500).end(); }
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  origin = `http://127.0.0.1:${server.address().port}`;
  return {
    origin, metadata, requests, artifacts: [...files.keys()],
    close: () => new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve())),
  };
}
