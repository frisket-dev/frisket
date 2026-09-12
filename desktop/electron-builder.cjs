const fs = require('node:fs');
const path = require('node:path');

const build = JSON.parse(fs.readFileSync(path.join(__dirname, 'resources/build.json'), 'utf8'));

module.exports = {
  appId: 'dev.frisket.desktop',
  productName: 'Frisket Desktop',
  extraMetadata: { version: build.version },
  asar: true,
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
    hardenedRuntime: Boolean(process.env.CSC_LINK),
    entitlements: 'entitlements.mac.plist',
    entitlementsInherit: 'entitlements.mac.plist',
    notarize: false,
    identity: process.env.CSC_LINK ? undefined : '-',
  },
  dmg: { sign: false },
};
