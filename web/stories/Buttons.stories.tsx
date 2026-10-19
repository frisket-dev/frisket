import type { Meta, StoryObj } from '@storybook/react-vite';
import { Plus, RefreshCw, Trash2, X } from 'lucide-react';
import { IconButton } from '../src/components/IconButton';
import { WindowCloseButton } from '../src/components/WindowCloseButton';
import { RemovableChip } from '../src/components/RemovableChip';
import { ActivityProgressCard } from '../src/components/ActivityProgress';

const meta = { title: 'Inventory/Buttons and close controls' } satisfies Meta;
export default meta;
type Story = StoryObj<typeof meta>;

export const SharedAndActivityClose: Story = {
  render: () => <div className="inventory-grid">
    <section className="inventory-example inventory-stack">
      <h2>Shared icon buttons</h2>
      <p>src/components/IconButton.tsx — actual size variants</p>
      <div className="inventory-row">
        <IconButton icon={X} label="Remove inline" size="inline" />
        <IconButton icon={Plus} label="Add compact" size="compact" />
        <IconButton icon={RefreshCw} label="Refresh standard" size="standard" />
        <IconButton icon={Trash2} label="Delete disabled" disabled />
      </div>
      <h2>Shared window close and removable chip</h2>
      <div className="inventory-row">
        <WindowCloseButton label="Close window" />
        <RemovableChip removeLabel="Remove label" onRemove={() => undefined}>Council</RemovableChip>
      </div>
    </section>
    <section className="inventory-example inventory-stack">
      <h2>Activity card dismiss</h2>
      <p>src/components/ActivityProgress.tsx — bespoke icon-btn treatment</p>
      <ActivityProgressCard state="complete" title="Import complete" detail="12 documents added"
        onDismiss={() => undefined} />
      <ActivityProgressCard state="error" title="Preparation failed" detail="Two documents need attention"
        onDismiss={() => undefined} />
    </section>
  </div>,
};
