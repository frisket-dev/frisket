import path from 'node:path';

export function executablePath(resources, name, platform = process.platform) {
  return path.join(resources, 'bin', `${name}${platform === 'win32' ? '.exe' : ''}`);
}

export function privatePythonPath(environment, platform = process.platform) {
  return platform === 'win32'
    ? path.join(environment, 'Scripts', 'python.exe')
    : path.join(environment, 'bin', 'python');
}

export function assertSupportedPlatform(platform = process.platform, arch = process.arch) {
  if (platform === 'darwin' && arch !== 'arm64') {
    throw new Error('Frisket desktop requires Apple Silicon on macOS.');
  }
  if (platform === 'win32' && arch !== 'x64') {
    throw new Error('Frisket desktop requires Windows x64.');
  }
  if (!['darwin', 'linux', 'win32'].includes(platform)) {
    throw new Error(`Unsupported desktop runtime platform: ${platform}/${arch}`);
  }
}
