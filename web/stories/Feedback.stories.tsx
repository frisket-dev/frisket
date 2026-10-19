import type { Meta, StoryObj } from '@storybook/react-vite';
import { PanelEmpty, PanelError, PanelLoading, StatusChip } from '../src/components/PanelPrimitives';
import { ActivityProgressCard, ProgressBar } from '../src/components/ActivityProgress';

const meta = { title: 'Inventory/Feedback and progress' } satisfies Meta;
export default meta;
type Story = StoryObj<typeof meta>;
export const SharedStates: Story = {
  render: () => <div className="inventory-grid">
    <section className="inventory-example inventory-stack">
      <h2>Shared panel states</h2><p>src/components/PanelPrimitives.tsx</p>
      <PanelEmpty>No sources yet</PanelEmpty><PanelLoading label="Loading sources…" />
      <PanelError>Unable to load sources. Try again.</PanelError>
      <div className="inventory-row">
        <StatusChip tone="success">Ready</StatusChip><StatusChip tone="warning">Needs attention</StatusChip>
        <StatusChip tone="error">Failed</StatusChip><StatusChip tone="info">Running</StatusChip>
      </div>
    </section>
    <section className="inventory-example inventory-stack">
      <h2>Activity progress</h2><p>src/components/ActivityProgress.tsx</p>
      <ActivityProgressCard state="active" title="Importing documents" detail="6 of 12 documents">
        <ProgressBar label="Import progress" completed={6} total={12} state="active" />
      </ActivityProgressCard>
      <ActivityProgressCard state="warning" title="Import finished with errors" detail="10 added, 2 failed" />
    </section>
  </div>,
};
