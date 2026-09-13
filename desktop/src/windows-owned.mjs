import { spawn } from 'node:child_process';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';

/** Windows first-use and backend ownership without a system Python dependency. */
export async function spawnWindowsOwned(command, args, { resourcesPath, parentPid = process.pid, env, stdio }) {
  if (typeof command !== 'string' || command.includes('\0') || !path.isAbsolute(command)
    || !Array.isArray(args) || args.some(arg => typeof arg !== 'string' || arg.includes('\0'))
    || !path.isAbsolute(resourcesPath) || !Number.isInteger(parentPid) || parentPid < 1) {
    throw new Error('invalid Windows runtime configuration');
  }
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'frisket-owned-'));
  const config = path.join(directory, 'launch.json');
  const stop = path.join(directory, 'stop');
  const proof = path.join(directory, 'clean');
  try {
    await fs.writeFile(config, JSON.stringify({ command, args, parentPid, stop, proof }), { mode: 0o600 });
    const systemRoot = process.env.SystemRoot || process.env.SYSTEMROOT;
    if (!systemRoot || !path.isAbsolute(systemRoot)) throw new Error('Windows system directory is unavailable.');
    const powershell = path.join(systemRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
    const child = spawn(powershell, ['-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File',
      path.join(resourcesPath, 'python', 'frisket', 'runtime', '_guard_windows.ps1'), '-Config', config,
    ], { env, stdio, windowsHide: true });
    child.windowsStopFile = stop;
    child.windowsCleanupProof = new Promise(resolve => {
      const completed = async () => {
        let proved = false;
        try { proved = await fs.readFile(proof, 'utf8') === 'clean'; } catch {}
        await fs.rm(directory, { recursive: true, force: true }).catch(() => {});
        resolve(proved);
      };
      child.once('close', completed);
    });
    return child;
  } catch (error) {
    await fs.rm(directory, { recursive: true, force: true });
    throw error;
  }
}

export async function requestWindowsStop(child) {
  await fs.writeFile(child.windowsStopFile, 'stop', { mode: 0o600 });
}
