const fs = require('node:fs');
const path = require('node:path');

const build = JSON.parse(fs.readFileSync(path.join(__dirname, 'resources/build.json'), 'utf8'));
const signedMacBuild = process.env.FRISKET_DESKTOP_SIGNED_BUILD === '1';
const signedWindowsBuild = process.env.FRISKET_DESKTOP_WINDOWS_SIGNED_BUILD === '1';

if (signedMacBuild) {
  const missing = ['CSC_LINK', 'APPLE_API_KEY', 'APPLE_API_KEY_ID', 'APPLE_API_ISSUER']
    .filter((name) => !process.env[name]);
  if (missing.length) {
    throw new Error(`Signed macOS packaging requires ${missing.join(', ')}`);
  }
}

function windowsAzureSignOptions() {
  if (!signedWindowsBuild) return undefined;
  const names = [
    'FRISKET_WINDOWS_SIGNING_PUBLISHER_NAME',
    'FRISKET_WINDOWS_SIGNING_ENDPOINT',
    'FRISKET_WINDOWS_SIGNING_ACCOUNT',
    'FRISKET_WINDOWS_SIGNING_PROFILE',
  ];
  const missing = names.filter((name) => !process.env[name]);
  if (missing.length) {
    throw new Error(`Signed Windows packaging requires ${missing.join(', ')}`);
  }
  return {
    publisherName: process.env.FRISKET_WINDOWS_SIGNING_PUBLISHER_NAME,
    endpoint: process.env.FRISKET_WINDOWS_SIGNING_ENDPOINT,
    codeSigningAccountName: process.env.FRISKET_WINDOWS_SIGNING_ACCOUNT,
    certificateProfileName: process.env.FRISKET_WINDOWS_SIGNING_PROFILE,
  };
}

module.exports = {
  appId: 'dev.frisket.desktop',
  productName: 'Frisket Desktop',
  extraMetadata: { version: build.version },
  publish: {
    provider: 'generic',
    url: 'https://github.com/frisket-dev/frisket/releases/latest/download/',
    channel: 'latest',
  },
  asar: true,
  // A signed build must never silently fall back to an unsigned bundle.
  forceCodeSigning: signedMacBuild || signedWindowsBuild,
  directories: { output: 'dist', buildResources: 'build' },
  files: ['src/**/*.mjs', 'ui/**/*', 'package.json'],
  extraResources: [{ from: 'resources', to: '.', filter: ['**/*'] }],
  artifactName: `Frisket-Desktop-\${version}-\${arch}-${build.revision.slice(0, 8)}.\${ext}`,
  mac: {
    icon: 'ui/icon.svg',
    target: [{ target: 'dmg', arch: ['arm64'] }, { target: 'zip', arch: ['arm64'] }],
    category: 'public.app-category.productivity',
    minimumSystemVersion: '14.0',
    // Ad-hoc signing cannot enforce a common Team ID across Electron frameworks.
    // The signed workflow supplies a Developer ID Application certificate.
    // Notarize and staple the app before archiving it for automatic updates.
    hardenedRuntime: signedMacBuild,
    entitlements: 'entitlements.mac.plist',
    entitlementsInherit: 'entitlements.mac.plist',
    notarize: signedMacBuild,
    identity: signedMacBuild ? undefined : '-',
  },
  win: {
    target: [{ target: 'nsis', arch: ['x64'] }],
    requestedExecutionLevel: 'asInvoker',
    signExecutable: signedWindowsBuild,
    ...(signedWindowsBuild ? { publisherName: process.env.FRISKET_WINDOWS_SIGNING_PUBLISHER_NAME } : {}),
    ...(signedWindowsBuild ? { azureSignOptions: windowsAzureSignOptions() } : {}),
  },
  nsis: {
    oneClick: true,
    perMachine: false,
    runAfterFinish: false,
  },
  // Sign the outer delivery container in signed builds, then notarize and
  // staple that final artifact exactly once in the workflow.
  dmg: { sign: signedMacBuild },
};
