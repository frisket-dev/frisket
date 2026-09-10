// The epoch/AbortController job lane. `state/jobStore.ts` uses independent
// instances to fence its private run, queued, and live-dock targets.
//
// Poll scheduling remains outside this primitive: each owning resource fixes
// its own cadence, visibility, and overlap policy. This module supplies only
// the epoch/abort identity around a target or one-shot async operation.

import type { Job, JobLane } from './types';

export function createJobLane(): JobLane {
  let epoch = 0;
  let ctrl: AbortController | null = null;

  return {
    start(): Job<unknown> {
      const jobEpoch = ++epoch;
      const candidate = new AbortController();
      const prior = ctrl;
      ctrl = null;
      prior?.abort();
      if (epoch !== jobEpoch || ctrl !== null) {
        candidate.abort();
        return { epoch: jobEpoch, signal: candidate.signal };
      }
      ctrl = candidate;
      return { epoch: jobEpoch, signal: candidate.signal };
    },
    isCurrent(job: Job<unknown>): boolean {
      return job.epoch === epoch;
    },
    cancel(): void {
      epoch += 1;
      const prior = ctrl;
      ctrl = null;
      prior?.abort();
    },
  };
}
