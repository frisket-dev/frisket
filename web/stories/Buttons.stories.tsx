import type { Meta, StoryObj } from '@storybook/react-vite';
import { Plus, RefreshCw, Trash2, X } from 'lucide-react';
import { IconButton } from '../src/components/IconButton';
import { WindowCloseButton } from '../src/components/WindowCloseButton';
import { RemovableChip } from '../src/components/RemovableChip';
import { ActivityProgressCard } from '../src/components/ActivityProgress';
import { Button, TabButton } from '../src/components/Button';

const meta = { title: 'Inventory/Buttons and close controls' } satisfies Meta;
export default meta;
type Story = StoryObj<typeof meta>;

export const SharedAndActivityClose: Story = {
  render: () => <div className="inventory-grid">
    <section className="inventory-example inventory-stack">
      <h2>Shared buttons and tabs</h2>
      <div className="inventory-row">
        <Button type="button">Cancel</Button>
        <Button type="button" className="btn-primary">Save</Button>
        <Button type="button" className="btn-reject"><Trash2 size={14} aria-hidden />Delete</Button>
        <Button type="button" disabled>Unavailable</Button>
      </div>
      <div className="tab-list" role="tablist" aria-label="Example tabs">
        <TabButton aria-selected>Documents</TabButton><TabButton aria-selected={false}>History</TabButton>
      </div>
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
      <p>src/components/ActivityProgress.tsx — shared WindowCloseButton</p>
      <ActivityProgressCard state="complete" title="Import complete" detail="12 documents added"
        onDismiss={() => undefined} />
      <ActivityProgressCard state="error" title="Preparation failed" detail="Two documents need attention"
        onDismiss={() => undefined} />
    </section>
  </div>,
};
