import { useEffect, useRef, useState } from 'react';
import type { Meta, StoryObj } from '@storybook/react-vite';
import { PanelSelect } from '../src/components/PanelSelect';
import { SegmentedToggle } from '../src/components/PanelPrimitives';

const options = [
  { value: 'source', label: 'Source document', description: 'Original imported file' },
  { value: 'summary', label: 'Summary', description: 'Generated column', ai: true },
  { value: 'long', label: 'A long column name to check narrow panel layouts and truncation' },
  { value: 'disabled', label: 'Unavailable column', disabled: true },
];

function SelectExample() {
  const [value, setValue] = useState('source');
  return <PanelSelect ariaLabel="Source column" options={options} value={value} onValueChange={setValue} />;
}

function SelectComparison() {
  const [mode, setMode] = useState('column');
  return <div className="inventory-grid inventory-popup-space">
    <section className="inventory-example inventory-stack inventory-narrow">
      <h2>Rich select at panel width</h2><p>src/components/PanelSelect.tsx</p>
      <SelectExample />
      <PanelSelect ariaLabel="Disabled source" options={options} value="source" disabled />
      <PanelSelect ariaLabel="Empty source" options={[]} value="" />
    </section>
    <section className="inventory-example inventory-stack inventory-narrow">
      <h2>Segmented choice</h2><p>src/components/PanelPrimitives.tsx</p>
      <SegmentedToggle options={[
        { value: 'column', label: 'Column' }, { value: 'template', label: 'Template' },
        { value: 'text', label: 'Text' },
      ]} value={mode} onValueChange={setMode} ariaLabel="Input mode" />
    </section>
  </div>;
}

function DialogSelect() {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    dialog.current?.showModal();
    const node = dialog.current;
    return () => node?.close();
  }, []);
  return <dialog ref={dialog} className="modal-card" aria-label="Dialog select example">
    <h2 className="modal-title">Dialog-owned dropdown</h2>
    <SelectExample />
  </dialog>;
}

const meta = { title: 'Inventory/Selects and menus' } satisfies Meta;
export default meta;
type Story = StoryObj<typeof meta>;
export const Comparison: Story = { render: () => <SelectComparison /> };
export const InDialog: Story = { render: () => <DialogSelect /> };
