import { useState } from 'react';
import type { Meta, StoryObj } from '@storybook/react-vite';
import type { OutputField } from '../src/api/open';
import { StructuredFieldsEditor } from '../src/components/action-panel/StructuredFieldsEditor';
import { FormField, SearchInput, TextArea, TextInput } from '../src/components/FormControls';

const meta = { title: 'Inventory/Structured fields' } satisfies Meta;
export default meta;
type Story = StoryObj<typeof meta>;

function Fields() {
  const [value, setValue] = useState<OutputField[]>([
    { name: 'person', type: 'text', description: 'Full name of the person', required: true },
    { name: 'amount', type: 'number', description: 'Total amount in USD' },
    { name: 'status', type: 'category', description: 'Decision', labels: ['approved', 'rejected', 'pending'] },
    { name: 'organizations', type: 'list', description: 'Organizations mentioned', items: { type: 'string' } },
  ]);
  return <section className="inventory-example" style={{ maxWidth: 420 }}>
    <h2>Structured output fields</h2>
    <p>Real structured output editor, including editable types, categories and list items.</p>
    <div className="action-panel"><div className="action-form">
      <StructuredFieldsEditor value={value} onChange={setValue} minFields={1} />
    </div></div>
  </section>;
}

export const MultipleTypes: Story = { render: () => <Fields /> };

export const NativeFields: Story = {
  render: () => <section className="inventory-example inventory-stack" style={{ maxWidth: 420 }}>
    <h2>Text fields</h2><p>The same native controls in actions, imports and settings.</p>
    <FormField label="Project name" hint="Label, control and help share one layout."><TextInput defaultValue="Council records" /></FormField>
    <SearchInput aria-label="Find documents" placeholder="Find documents" />
    <TextInput aria-label="Standard input" placeholder="Standard input" />
    <TextInput aria-label="Compact input" density="compact" placeholder="Compact input" />
    <TextInput aria-label="Disabled input" disabled value="Disabled input" />
    <TextInput aria-label="Invalid input" aria-invalid="true" className="form-input-invalid" defaultValue="Invalid input" />
    <TextArea aria-label="Multiline input" placeholder="Multiline text" />
  </section>,
};
