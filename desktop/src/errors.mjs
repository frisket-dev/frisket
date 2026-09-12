/** A failed process cleanup makes another launch unsafe in this session. */
export class CleanupError extends Error {
  constructor(message) {
    super(message);
    this.name = 'CleanupError';
  }
}
