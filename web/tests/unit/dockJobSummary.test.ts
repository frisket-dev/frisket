// Plugin load/runtime failures route into the
// Errors dock instead of the retired Plugins dock tab. derivePluginErrorJobs
// is the pure derivation that turns the already-fetched runtime index into
// synthetic error-dock rows (same shape/technique as the pre-existing
// syntheticErrorJobFromRun for direct-run failures) — this pins its filter
// (installState/activation 'failed' only, never healthy/disabled-by-choice
// plugins) and its message precedence (installFailure.message >
// disabledReason > a generic fallback).

import { describe, expect, it } from 'vitest';
import {
  derivePluginErrorJobs,
  isErrorJob,
  mergeDockJobs,
} from '../../src/workbench/dockJobSummary';
import type { WorkbenchPluginRuntimeIndex, WorkbenchPluginRuntimePlugin } from '../../src/api/types';
import type { DockActionJob } from '../../src/workbench/WorkbenchBottomDock';

function dockJob(
  jobId: number,
  createdAt: string,
  actionName: string,
  startedAt: string | null = null,
): DockActionJob {
  return {
    jobId,
    actionName,
    timing: { createdAt, startedAt, finishedAt: null },
  } as DockActionJob;
}

describe('mergeDockJobs', () => {
  it('uses the dock winner, retains base-only rows, and sorts newest first', () => {
    const baseOnly = dockJob(1, '2026-08-08T00:00:00Z', 'base-only');
    const baseLoser = dockJob(2, '2026-08-08T00:00:01Z', 'base-loser');
    const dockWinner = dockJob(2, '2026-08-08T00:00:01Z', 'dock-winner');
    const newest = dockJob(3, '2026-08-08T00:00:02Z', 'newest');

    expect(mergeDockJobs([baseOnly, baseLoser], [dockWinner, newest])).toEqual([
      newest,
      dockWinner,
      baseOnly,
    ]);
  });

  it('sorts a base-only projection by started-at then created-at', () => {
    const createdNewer = dockJob(1, '2026-08-08T00:00:02Z', 'created-newer');
    const startedNewest = dockJob(
      2,
      '2026-08-08T00:00:00Z',
      'started-newest',
      '2026-08-08T00:00:03Z',
    );
    expect(mergeDockJobs([createdNewer, startedNewest], null)).toEqual([
      startedNewest,
      createdNewer,
    ]);
  });
});

function plugin(overrides: Partial<WorkbenchPluginRuntimePlugin> = {}): WorkbenchPluginRuntimePlugin {
  return {
    schemaVersion: 'frisket.workbench_plugin_runtime_plugin.v1',
    pluginId: 'frisket.example',
    version: '0.1.0',
    installState: 'installed',
    activation: 'manifestLoaded',
    runtimeSource: 'plugin.load_receipt',
    receiptId: 'receipt_1',
    manifestSha256: 'sha256:x',
    byteCount: 1,
    source: { kind: 'local_file' },
    contributionSummary: [],
    requires: { capabilities: [], secrets: [] },
    arbitraryPackageLoadAllowed: false,
    ...overrides,
  } as WorkbenchPluginRuntimePlugin;
}

function index(plugins: WorkbenchPluginRuntimePlugin[]): WorkbenchPluginRuntimeIndex {
  return {
    schemaVersion: 'frisket.workbench_plugin_runtime_index.v1',
    projectId: 'proj_1',
    arbitraryPackageLoadAllowed: false,
    receiptScanLimit: 5000,
    skippedInvalidReceipts: 0,
    skippedInvalidManifestRefs: 0,
    loadedPluginCount: plugins.length,
    plugins,
    firstParty: { schemaVersion: 'frisket.workbench_descriptor_package.v1', descriptors: [] },
  };
}

describe('derivePluginErrorJobs', () => {
  it('returns nothing for a null runtime index (never fetched yet)', () => {
    expect(derivePluginErrorJobs(null)).toEqual([]);
  });

  it('returns nothing when every plugin is healthy', () => {
    const idx = index([plugin({ pluginId: 'frisket.geo', installState: 'enabled', activation: 'registryManifestRegistered' })]);
    expect(derivePluginErrorJobs(idx)).toEqual([]);
  });

  it('ignores a disabled-by-choice plugin (not a failure)', () => {
    const idx = index([
      plugin({ pluginId: 'frisket.geo', installState: 'disabled', activation: 'blocked', disabledReason: 'plugin_disabled' }),
    ]);
    expect(derivePluginErrorJobs(idx)).toEqual([]);
  });

  it('surfaces a plugin with installState "failed" as an error-dock row', () => {
    const idx = index([
      plugin({
        pluginId: 'frisket.broken',
        installState: 'failed',
        activation: 'failed',
        installFailure: { code: 'manifest_invalid', message: 'workbench-descriptors.json failed schema validation' },
      }),
    ]);
    const rows = derivePluginErrorJobs(idx);
    expect(rows).toHaveLength(1);
    const row = rows[0];
    expect(row.kind).toBe('plugin');
    expect(row.projectId).toBe('proj_1');
    expect(row.actionName).toBe('Plugin: frisket.broken');
    expect(row.error).toBe('workbench-descriptors.json failed schema validation');
    expect(row.resultSummary).toMatchObject({
      pluginId: 'frisket.broken',
      installState: 'failed',
      activation: 'failed',
      failureCode: 'manifest_invalid',
    });
    expect(isErrorJob(row)).toBe(true);
  });

  it('falls back to disabledReason, then a generic message, when installFailure is absent', () => {
    const withDisabledReason = derivePluginErrorJobs(
      index([plugin({ pluginId: 'a', installState: 'failed', disabledReason: 'quarantined' })]),
    )[0];
    expect(withDisabledReason.error).toBe('quarantined');

    const generic = derivePluginErrorJobs(
      index([plugin({ pluginId: 'b', installState: 'failed' })]),
    )[0];
    expect(generic.error).toBe('b failed to install.');

    const activationGeneric = derivePluginErrorJobs(
      index([plugin({ pluginId: 'c', installState: 'installed', activation: 'failed' })]),
    )[0];
    expect(activationGeneric.error).toBe('c failed to activate.');
  });

  it('assigns distinct negative job ids across multiple failed plugins whose ids do not hash-collide', () => {
    const rows = derivePluginErrorJobs(
      index([
        plugin({ pluginId: 'frisket.one', installState: 'failed' }),
        plugin({ pluginId: 'frisket.two', installState: 'failed' }),
      ]),
    );
    expect(rows).toHaveLength(2);
    expect(rows[0].jobId).not.toBe(rows[1].jobId);
    for (const row of rows) expect(row.jobId).toBeLessThan(0);
  });

  it('de-duplicates a genuine hash collision instead of dropping one row', () => {
    // 'demo.1n' and 'demo.30' are a concrete collision pair:
    // both canonical plugin ids (frisket/contracts/plugin.py grammar), both
    // hash to jobId -1551615560 under the 32-bit polynomial seed
    // (pluginErrorJobIdSeed). Without de-duplication, mergeDockJobs' plain
    // Map<number, DockActionJob> (WorkbenchBottomDock.tsx) would silently
    // collapse these into one row and the Errors badge would over-count.
    const rows = derivePluginErrorJobs(
      index([
        plugin({ pluginId: 'demo.1n', installState: 'failed' }),
        plugin({ pluginId: 'demo.30', installState: 'failed' }),
      ]),
    );
    expect(rows).toHaveLength(2);
    const jobIds = rows.map((row) => row.jobId);
    expect(new Set(jobIds).size).toBe(2);
    const pluginIds = rows.map((row) => row.resultSummary?.pluginId);
    expect(pluginIds.sort()).toEqual(['demo.1n', 'demo.30']);
  });

  it('never drops a row across a larger batch, regardless of whether any seeds happen to collide', () => {
    // These 25 ids are NOT a crafted colliding set (the 'demo.1n'/'demo.30'
    // case above is the actual probing exercise) — this instead pins the
    // general guarantee derivePluginErrorJobs makes independent of hash
    // quality: N failed plugins always yields N rows with N distinct job
    // ids, never fewer, whether or not this particular batch's seeds
    // collide with each other.
    const plugins = Array.from({ length: 25 }, (_, i) =>
      plugin({ pluginId: `frisket.batch_${i}`, installState: 'failed' }),
    );
    const rows = derivePluginErrorJobs(index(plugins));
    expect(rows).toHaveLength(25);
    expect(new Set(rows.map((row) => row.jobId)).size).toBe(25);
  });
});
