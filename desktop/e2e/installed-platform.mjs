import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import path from 'node:path';

const exec = promisify(execFile);

export function assertInstalledPlatform() {
  const supported = (process.platform === 'darwin' && process.arch === 'arm64')
    || (process.platform === 'win32' && process.arch === 'x64');
  if (!supported) {
    throw new Error(`Installed desktop proof does not support ${process.platform}/${process.arch}.`);
  }
}

export function installedExecutable(appPath) {
  return process.platform === 'darwin'
    ? path.join(appPath, 'Contents', 'MacOS', 'Frisket Desktop')
    : appPath;
}

export function installedResources(appPath) {
  return process.platform === 'darwin'
    ? path.join(appPath, 'Contents', 'Resources')
    : path.join(path.dirname(appPath), 'resources');
}

async function powershellJson(script) {
  const { stdout } = await exec('powershell.exe', [
    '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', script,
  ]);
  const value = stdout.trim();
  return value ? JSON.parse(value) : [];
}

export async function descendants(pid) {
  if (process.platform === 'win32') return selectDescendants(await windowsProcessDetails(), pid);
  const { stdout } = await exec('/bin/ps', ['-axo', 'pid=,ppid=']);
  const rows = stdout.trim().split('\n').filter(Boolean).map((line) => {
    const [pid, parent] = line.trim().split(/\s+/).map(Number);
    return { pid, parent };
  });
  return selectDescendants(rows, pid);
}

export function selectDescendants(rows, pid) {
  const root = rows.find((row) => row.pid === pid);
  if (!root) throw new Error(`Process ${pid} disappeared before its descendants could be recorded.`);
  if ('started' in root && !root.started) throw new Error(`Process ${pid} has no creation time.`);
  const owned = new Map([[pid, root]]);
  for (let previous = -1; previous !== owned.size;) {
    previous = owned.size;
    for (const row of rows) {
      const parent = owned.get(row.parent);
      if (parent?.started && !row.started) throw new Error(`Process ${row.pid} has no creation time.`);
      // Windows retains ParentProcessId after the parent dies. A process older
      // than its purported parent belongs to a previous occupant of that PID.
      if (parent && (!parent.started || row.started >= parent.started)) owned.set(row.pid, row);
    }
  }
  return [...owned.values()];
}

export async function listeningPorts(pids) {
  if (!pids.length) return [];
  if (process.platform !== 'win32') {
    const { stdout } = await exec('/usr/sbin/lsof', [
      '-nP', '-a', '-p', pids.join(','), '-iTCP', '-sTCP:LISTEN', '-Fn',
    ]);
    return [...stdout.matchAll(/^n127\.0\.0\.1:(\d+)$/gm)].map((match) => Number(match[1]));
  }
  const ids = pids.map(Number).join(',');
  const script = `
    $owned = @(${ids})
    @(Get-NetTCPConnection -State Listen -ErrorAction Stop |
      Where-Object { $owned -contains [int]$_.OwningProcess -and $_.LocalAddress -in @('127.0.0.1', '::1') } |
      ForEach-Object { [int]$_.LocalPort } | Sort-Object -Unique) | ConvertTo-Json -Compress
  `;
  const result = await powershellJson(script);
  return (Array.isArray(result) ? result : [result]).filter(Number.isFinite);
}

export async function aliveProcesses(processes) {
  if (!processes.length) return [];
  if (process.platform !== 'win32') {
    return processes.filter(({ pid }) => {
      try { process.kill(pid, 0); return true; } catch { return false; }
    });
  }
  const current = await windowsProcessDetails(processes.map(({ pid }) => pid), true);
  return processes.filter((record) => current.some((candidate) =>
    candidate.pid === record.pid && candidate.started === record.started));
}

/** Identify Windows process instances without logging arguments or credentials. */
export async function windowsProcessDetails(pids, requireAlive = false) {
  if (process.platform !== 'win32' || pids?.length === 0) return [];
  const ids = pids?.map(Number).join(',') || '';
  const result = await powershellJson(`
    $ids = @(${ids})
    @(Get-CimInstance -ClassName Win32_Process | Where-Object { $ids.Count -eq 0 -or $ids -contains [int]$_.ProcessId } |
      ForEach-Object {
        $row = $_
        if (${requireAlive ? '$true' : '$false'}) {
          try {
            $process = Get-Process -Id $row.ProcessId -ErrorAction Stop
          } catch {
            if ($_.FullyQualifiedErrorId -like 'NoProcessFoundForGivenId*') { return }
            throw
          }
          if ($process.HasExited) { return }
        }
        [PSCustomObject]@{
          pid = [int]$row.ProcessId
          parent = [int]$row.ParentProcessId
          name = $row.Name
          started = if ($row.CreationDate) { $row.CreationDate.ToUniversalTime().ToString('O') } else { $null }
        }
      }) | ConvertTo-Json -Compress
  `);
  return Array.isArray(result) ? result : [result];
}

export async function runningExecutable(executable) {
  if (process.platform !== 'win32') return [];
  if (!path.isAbsolute(executable)) throw new Error('Expected an absolute executable path.');
  const literal = executable.replaceAll("'", "''");
  const result = await powershellJson(`
    @(Get-CimInstance -ClassName Win32_Process |
      Where-Object { $_.ExecutablePath -eq '${literal}' } |
      ForEach-Object { [int]$_.ProcessId }) | ConvertTo-Json -Compress
  `);
  return Array.isArray(result) ? result : [result];
}
