const fs = require('node:fs');
const path = require('node:path');

const build = JSON.parse(fs.readFileSync(path.join(__dirname, 'resources/build.json'), 'utf8'));
const signedBuild = process.env.FRISKET_DESKTOP_SIGNED_BUILD === '1';

if (signedBuild) {
  const missing = ['CSC_LINK', 'APPLE_API_KEY', 'APPLE_API_KEY_ID', 'APPLE_API_ISSUER']
    .filter((name) => !process.env[name]);
  if (missing.length) {
    throw new Error(`Signed macOS packaging requires ${missing.join(', ')}`);
  }
}

module.exports = {
  appId: 'dev.frisket.desktop',
  productName: 'Frisket Desktop',
  extraMetadata: { version: build.version },
  asar: true,
  // A signed build must never silently fall back to an unsigned bundle.
  forceCodeSigning: signedBuild,
  directories: { output: 'dist', buildResources: 'build' },
  files: ['src/**/*.mjs', 'ui/**/*', 'package.json'],
  extraResources: [{ from: 'resources', to: '.', filter: ['**/*'] }],
  artifactName: `Frisket-Desktop-\${version}-\${arch}-${build.revision.slice(0, 8)}.\${ext}`,
  mac: {
    icon: 'ui/icon.svg',
    target: [{ target: 'dmg', arch: ['arm64'] }],
    category: 'public.app-category.productivity',
    minimumSystemVersion: '14.0',
    // Ad-hoc signing cannot enforce a common Team ID across Electron frameworks.
    // The signed workflow supplies a Developer ID Application certificate.
    // It notarizes the final DMG, rather than this intermediate app.
    hardenedRuntime: signedBuild,
    entitlements: 'entitlements.mac.plist',
    entitlementsInherit: 'entitlements.mac.plist',
    notarize: false,
    identity: signedBuild ? undefined : '-',
  },
  // Sign the outer delivery container in signed builds, then notarize and
  // staple that final artifact exactly once in the workflow.
  dmg: { sign: signedBuild },
};
