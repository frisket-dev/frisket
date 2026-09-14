import { createHash } from 'node:crypto';
import { createReadStream } from 'node:fs';
import { copyFile, mkdir, readFile, readdir, stat, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const repository = 'frisket-dev/frisket';
const channels = { mac: 'latest-mac.yml', windows: 'latest.yml' };

export function verifyConfiguration(configuration, expectedPublisher) {
  if (configuration.provider !== 'generic' || configuration.channel !== 'latest'
      || configuration.url !== `https://github.com/${repository}/releases/latest/download/`) {
    throw new Error('Packaged updater configuration does not use the public latest feed');
  }
  if (expectedPublisher && ![].concat(configuration.publisherName ?? []).includes(expectedPublisher)) {
    throw new Error('Packaged updater configuration does not enforce the signing publisher');
  }
}

async function digest(file, algorithm, encoding) {
  const hash = createHash(algorithm);
  for await (const chunk of createReadStream(file)) hash.update(chunk);
  return hash.digest(encoding);
}

function assetName(platform, build, extension) {
  if (!/^[0-9a-f]{40}$/.test(build.revision)
      || !/^\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)\.\d+)?$/.test(build.version)
      || !/^\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?$/.test(build.pythonPackageVersion)) {
    throw new Error('Invalid release build metadata');
  }
  return `Frisket-Desktop-${build.version}-${platform === 'mac' ? 'arm64' : 'x64'}-${build.revision.slice(0, 8)}.${extension}`;
}

function assetUrl(name, build) {
  return `https://github.com/${repository}/releases/download/desktop-v${build.pythonPackageVersion}/${name}`;
}

// Only updater payloads belong in this metadata. The manual DMG is notarized
// and stapled after packaging, so its original builder hash is no longer valid.
export async function prepareMetadata(platform, directory, build, metadata) {
  if (!channels[platform]) throw new Error('Unsupported updater platform');
  const name = assetName(platform, build, platform === 'mac' ? 'zip' : 'exe');
  if (metadata.version !== build.version) throw new Error('Updater version differs from release build');
  const entries = metadata.files?.filter((file) => file.url === name);
  if (entries?.length !== 1) throw new Error(`Expected one updater payload named ${name}`);
  const payload = path.join(directory, name);
  const sha512 = await digest(payload, 'sha512', 'base64');
  const size = (await stat(payload)).size;
  if (entries[0].sha512 !== sha512 || (entries[0].size != null && entries[0].size !== size)) {
    throw new Error(`Updater payload hash or size differs: ${name}`);
  }
  return {
    version: build.version,
    files: [{ ...entries[0], url: assetUrl(name, build), sha512, size }],
    path: assetUrl(name, build),
    sha512,
    ...(metadata.releaseDate ? { releaseDate: metadata.releaseDate } : {}),
    ...(metadata.minimumSystemVersion ? { minimumSystemVersion: metadata.minimumSystemVersion } : {}),
  };
}

export async function verifyMetadata(platform, directory, build, metadata) {
  const name = assetName(platform, build, platform === 'mac' ? 'zip' : 'exe');
  if (metadata.files?.length !== 1 || metadata.files[0].url !== assetUrl(name, build)
      || metadata.path !== assetUrl(name, build) || metadata.sha512 !== metadata.files[0].sha512) {
    throw new Error('Updater metadata does not reference the immutable release payload');
  }
  await prepareMetadata(platform, directory, build, {
    ...metadata, files: [{ ...metadata.files[0], url: name }],
  });
  return name;
}

export async function stageRelease(macDirectory, windowsDirectory, destination, build) {
  await mkdir(destination, { recursive: true });
  if ((await readdir(destination)).length) throw new Error('Release staging directory must be empty');
  const names = [];
  for (const [platform, directory] of [['mac', macDirectory], ['windows', windowsDirectory]]) {
    const channel = channels[platform];
    const metadata = JSON.parse(await readFile(path.join(directory, channel), 'utf8'));
    const name = await verifyMetadata(platform, directory, build, metadata);
    await copyFile(path.join(directory, name), path.join(destination, name));
    names.push(name);
    // Preserve generated blockmaps when available; full downloads also work.
    const blockmap = `${name}.blockmap`;
    if ((await readdir(directory)).includes(blockmap)) {
      await copyFile(path.join(directory, blockmap), path.join(destination, blockmap));
      names.push(blockmap);
    }
    await writeFile(path.join(destination, channel), `${JSON.stringify(metadata, null, 2)}\n`);
    names.push(channel);
    const manualName = platform === 'mac' ? assetName(platform, build, 'dmg') : name;
    const alias = platform === 'mac' ? 'Frisket-Desktop-arm64.dmg' : 'Frisket-Desktop-x64.exe';
    await copyFile(path.join(directory, manualName), path.join(destination, alias));
    names.push(alias);
  }
  const checksums = [];
  for (const name of names.sort()) {
    checksums.push(`${await digest(path.join(destination, name), 'sha256', 'hex')}  ${name}`);
  }
  await writeFile(path.join(destination, 'SHA256SUMS'), `${checksums.join('\n')}\n`);
  return [...names, 'SHA256SUMS'];
}

async function main() {
  const [command, ...args] = process.argv.slice(2);
  if (command === 'prepare' && [3, 4].includes(args.length)) {
    const [platform, directory, buildFile, expectedPublisher] = args;
    if (!channels[platform]) throw new Error('Unsupported updater platform');
    const require = createRequire(import.meta.url);
    const { load } = require('js-yaml');
    const configurationFile = platform === 'mac'
      ? path.join(directory, 'mac-arm64/Frisket Desktop.app/Contents/Resources/app-update.yml')
      : path.join(directory, 'win-unpacked/resources/app-update.yml');
    verifyConfiguration(load(await readFile(configurationFile, 'utf8')), expectedPublisher);
    const build = JSON.parse(await readFile(buildFile, 'utf8'));
    const file = path.join(directory, channels[platform]);
    const metadata = await prepareMetadata(platform, directory, build, load(await readFile(file, 'utf8')));
    // JSON is valid YAML; publication can validate it without installing a parser.
    await writeFile(file, `${JSON.stringify(metadata, null, 2)}\n`);
  } else if (command === 'release' && args.length === 6) {
    const [macDirectory, windowsDirectory, destination, version, pythonPackageVersion, revision] = args;
    await stageRelease(macDirectory, windowsDirectory, destination, { version, pythonPackageVersion, revision });
  } else {
    throw new Error('Usage: stage-update.mjs prepare <mac|windows> <dist> <build.json> | release <mac-dist> <windows-dist> <destination> <version> <package-version> <revision>');
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  await main();
}
