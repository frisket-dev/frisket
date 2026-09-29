import type { CellValue, ReviewAction, ReviewBundle, ReviewBundleField } from '../../api/types';

export function isDecided(field: ReviewBundleField): boolean {
  return field.reviewState !== 'unreviewed';
}

export function decisionLabel(field: ReviewBundleField): string {
  if (field.reviewDecision === 'edit') return 'Corrected';
  if (field.reviewState === 'verified') return 'Accepted';
  if (field.reviewState === 'rejected') return 'Rejected';
  return 'Unreviewed';
}

export function applyDecision(field: ReviewBundleField, action: ReviewAction, value?: CellValue): ReviewBundleField {
  if (action === 'clear') return { ...field, reviewDecision: null, reviewState: 'unreviewed', chore: true };
  const rejected = action === 'reject' || action === 'reject_clear';
  return {
    ...field,
    reviewDecision: action,
    reviewState: rejected ? 'rejected' : 'verified',
    chore: false,
    ...(action === 'edit' ? { value: value ?? null, changed: true } : {}),
    ...(action === 'reject_clear' ? { value: null, changed: true } : {}),
  };
}

export function nextUndecidedField(bundle: ReviewBundle, afterId: string): string {
  const index = bundle.fields.findIndex((field) => field.id === afterId);
  for (let step = 1; step <= bundle.fields.length; step += 1) {
    const field = bundle.fields[(index + step) % bundle.fields.length];
    if (!isDecided(field)) return field.id;
  }
  return afterId;
}
