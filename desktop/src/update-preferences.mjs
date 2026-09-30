import { readFileSync, renameSync, writeFileSync } from 'node:fs';

/** One preference in the writable profile, never inside the signed app. */
export function updatePreferences(filename) {
  let skippedVersion;
  try {
    const saved = JSON.parse(readFileSync(filename, 'utf8'));
    if (typeof saved?.skippedVersion === 'string') skippedVersion = saved.skippedVersion;
  } catch (error) {
    if (error.code !== 'ENOENT') console.warn('Could not read Desktop update preference:', error.message);
  }
  return {
    getSkippedVersion: () => skippedVersion,
    skipVersion(version) {
      const temporary = `${filename}.tmp`;
      writeFileSync(temporary, JSON.stringify({ skippedVersion: version }), { mode: 0o600 });
      renameSync(temporary, filename);
      skippedVersion = version;
    },
  };
}
