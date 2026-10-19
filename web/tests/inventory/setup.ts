import { mkdirSync, rmSync } from 'node:fs';
import path from 'node:path';

export default function setup() {
  // Only generated inventory artifacts; never a user workspace or model cache.
  const output = path.resolve(process.env.FRISKET_INVENTORY_OUTPUT ?? 'screenshots/inventory');
  if (!output.startsWith(`${path.resolve('screenshots')}${path.sep}`)) {
    throw new Error('Inventory output must be a subdirectory of web/screenshots');
  }
  rmSync(output, { recursive: true, force: true });
  mkdirSync(output, { recursive: true });
}
