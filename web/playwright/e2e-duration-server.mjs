#!/usr/bin/env node
import { spawn } from 'node:child_process';
import { appendDurationEvent, safeDiagnosticText } from '../e2e-duration-diagnostics.mjs';

function parseArgs(argv) {
  const split = argv.indexOf('--');
  const own = split >= 0 ? argv.slice(0, split) : argv;
  const command = split >= 0 ? argv.slice(split + 1) : [];
  const args = {
    phase: '',
    name: '',
    url: '',
    timeoutMs: 30_000,
    command,
  };
  for (let index = 0; index < own.length; index += 1) {
    const flag = own[index];
    if (flag === '--phase') args.phase = own[++index] || '';
    else if (flag === '--name') args.name = own[++index] || '';
    else if (flag === '--url') args.url = own[++index] || '';
    else if (flag === '--timeout') args.timeoutMs = Number(own[++index] || 0);
  }
  if (!args.phase || !args.name || !args.url || args.command.length === 0) {
    throw new Error(
      'Usage: e2e-duration-server.mjs --phase <phase> --name <name> --url <url> -- <command...>',
    );
  }
  if (!Number.isFinite(args.timeoutMs) || args.timeoutMs <= 0) args.timeoutMs = 30_000;
  return args;
}

function childCommand(command) {
  const env = { ...process.env };
  const args = [...command];
  while (args.length > 0 && /^[A-Za-z_][A-Za-z0-9_]*=/.test(args[0])) {
    const raw = args.shift();
    const splitAt = raw.indexOf('=');
    env[raw.slice(0, splitAt)] = raw.slice(splitAt + 1);
  }
  if (args.length === 0) {
    throw new Error('Duration wrapper command contained only environment assignments');
  }
  return { env, args };
}

const STDERR_TAIL_LIMIT = 50;

function sleep(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

function isLikelyOom(code, signal) {
  return signal === 'SIGKILL' || code === 137;
}

function formatUnexpectedExit({ name, when, code, signal, stderrLines }) {
  const details = [`code=${code ?? 'null'}`];
  if (signal) details.push(`signal=${signal}`);
  const oomHint = isLikelyOom(code, signal)
    ? ' — likely OOM kill (SIGKILL / exit 137)'
    : '';
  const header =
    `[e2e-server] ${safeDiagnosticText(name, 80)} ${when} ` +
    `(${details.join(', ')})${oomHint}`;
  if (!stderrLines || stderrLines.length === 0) {
    return `${header}\n[e2e-server] (no stderr captured)`;
  }
  const tail = stderrLines.map((line) => `  | ${line}`).join('\n');
  return (
    `${header}\n` +
    `[e2e-server] Last ${stderrLines.length} stderr line(s):\n` +
    `${tail}`
  );
}

function writeStderrAndFlush(text) {
  return new Promise((resolve) => {
    process.stderr.write(`${text}\n`, () => resolve());
  });
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const childSpec = childCommand(args.command);
  const child = spawn(childSpec.args[0], childSpec.args.slice(1), {
    env: childSpec.env,
    stdio: ['inherit', 'inherit', 'pipe'],
  });
  const stderrTail = [];
  let stderrPending = '';
  if (child.stderr) {
    child.stderr.on('data', (chunk) => {
      process.stderr.write(chunk);
      const text = typeof chunk === 'string' ? chunk : chunk.toString('utf8');
      stderrPending += text;
      const lines = stderrPending.split(/\r?\n/);
      stderrPending = lines.pop() ?? '';
      for (const line of lines) {
        stderrTail.push(line);
        if (stderrTail.length > STDERR_TAIL_LIMIT) {
          stderrTail.shift();
        }
      }
    });
  }
  const recentStderrLines = () => {
    const lines = stderrPending ? [...stderrTail, stderrPending] : [...stderrTail];
    return lines.slice(-STDERR_TAIL_LIMIT);
  };

  let childExited = false;
  let childExitCode = null;
  let childExitSignal = null;
  let forwardedSignal = null;
  let readinessFailed = false;
  const childDone = new Promise((resolve) => {
    child.once('exit', (code, signal) => {
      childExitCode = code;
      childExitSignal = signal;
      childExited = true;
      resolve({ code, signal });
    });
  });
  const childClosed = new Promise((resolve) => {
    child.once('close', (code, signal) => {
      resolve({ code, signal });
    });
  });

  const forwardSignal = (signal) => {
    forwardedSignal = signal;
    if (!childExited) child.kill(signal);
  };
  process.once('SIGINT', () => forwardSignal('SIGINT'));
  process.once('SIGTERM', () => forwardSignal('SIGTERM'));

  async function stopChildForFailure() {
    if (childExited) return;
    child.kill('SIGTERM');
    const killer = setTimeout(() => {
      if (!childExited) child.kill('SIGKILL');
    }, 1000);
    try {
      await childDone;
    } finally {
      clearTimeout(killer);
    }
  }

  try {
    const started = Date.now();
    let elapsedSeconds = null;
    let lastError = '';
    while (Date.now() - started < args.timeoutMs) {
      if (childExited) {
        await Promise.race([childClosed, sleep(250)]);
        throw new Error(
          formatUnexpectedExit({
            name: args.name,
            when: 'exited before readiness',
            code: childExitCode,
            signal: childExitSignal,
            stderrLines: recentStderrLines(),
          }),
        );
      }
      try {
        const response = await fetch(args.url);
        if (response.ok) {
          elapsedSeconds = (Date.now() - started) / 1000;
          break;
        }
      } catch (error) {
        lastError = error instanceof Error ? error.message : String(error);
      }
      await sleep(250);
    }
    if (elapsedSeconds === null) {
      throw new Error(
        `Timed out waiting for ${safeDiagnosticText(args.url, 160)}: ${
          safeDiagnosticText(lastError, 160)
        }`,
      );
    }
    if (process.env.FRISKET_E2E_DURATION_DISABLE !== '1') {
      appendDurationEvent({
        phase: args.phase,
        name: args.name,
        elapsed_seconds: elapsedSeconds,
      });
    }
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    readinessFailed = true;
    await stopChildForFailure();
    process.exitCode = 1;
  }

  if (readinessFailed) {
    process.exitCode = 1;
    return;
  }
  if (forwardedSignal) {
    process.kill(process.pid, forwardedSignal);
    return;
  }
  const childResult = await childDone;
  if (!forwardedSignal) {
    await Promise.race([childClosed, sleep(250)]);
    await writeStderrAndFlush(
      formatUnexpectedExit({
        name: args.name,
        when: 'exited unexpectedly before teardown',
        code: childResult.code,
        signal: childResult.signal,
        stderrLines: recentStderrLines(),
      }),
    );
  }
  if (childResult.signal) {
    process.kill(process.pid, childResult.signal);
    return;
  }
  process.exitCode = childResult.code ?? 1;
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
});
