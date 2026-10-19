import { mkdirSync, rmSync } from 'node:fs';
import path from 'node:path';

export default function setup() {
  // Only generated inventory artifacts; never a user workspace or model cache.
  const output = path.resolve('screenshots/inventory');
  rmSync(output, { recursive: true, force: true });
  mkdirSync(output, { recursive: true });
}
