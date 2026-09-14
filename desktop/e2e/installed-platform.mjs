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
  if (process.platform !== 'win32') {
    const { stdout } = await exec('/bin/ps', ['-axo', 'pid=,ppid=']);
    const rows = stdout.trim().split('\n').filter(Boolean)
      .map((line) => line.trim().split(/\s+/).map(Number));
    const owned = new Set([pid]);
    for (let previous = -1; previous !== owned.size;) {
      previous = owned.size;
      for (const [child, parent] of rows) if (owned.has(parent)) owned.add(child);
    }
    return [...owned];
  }
  const script = `
    $owned = [System.Collections.Generic.HashSet[int]]::new()
    [void]$owned.Add(${Number(pid)})
    $rows = Get-CimInstance -ClassName Win32_Process | ForEach-Object {
      [PSCustomObject]@{ pid = [int]$_.ProcessId; parent = [int]$_.ParentProcessId }
    }
    do {
      $before = $owned.Count
      foreach ($row in $rows) { if ($owned.Contains($row.parent)) { [void]$owned.Add($row.pid) } }
    } while ($before -ne $owned.Count)
    @($owned | Sort-Object) | ConvertTo-Json -Compress
  `;
  const result = await powershellJson(script);
  return Array.isArray(result) ? result : [result];
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

export async function alivePids(pids) {
  if (!pids.length) return [];
  if (process.platform !== 'win32') {
    return pids.filter((candidate) => {
      try { process.kill(candidate, 0); return true; } catch { return false; }
    });
  }
  const ids = pids.map(Number).join(',');
  const script = `
    @(${ids}) | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue } | ConvertTo-Json -Compress
  `;
  const result = await powershellJson(script);
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
