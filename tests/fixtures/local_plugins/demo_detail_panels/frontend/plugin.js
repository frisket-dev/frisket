const detailStamp = (testId) => ({ React, ctx }) =>
  React.createElement('section', {
    'data-testid': testId,
    'data-schema': ctx?.schemaVersion ?? 'missing',
    'data-contribution-id': ctx?.contributionId ?? 'missing',
    'data-subject-kind': ctx?.detail?.subject?.kind ?? 'missing',
    'data-subject': JSON.stringify(ctx?.detail?.subject ?? null),
  }, 'demo detail contribution');

export const RowTab = detailStamp('demo-detail-row-tab');
export const ColumnSection = detailStamp('demo-detail-column-section');
export const EntityTab = detailStamp('demo-detail-entity-tab');
export const SourceTab = detailStamp('demo-detail-source-tab');

export const ColumnTab = detailStamp('demo-detail-column-tab');
