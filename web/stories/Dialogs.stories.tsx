import type { Meta, StoryObj } from '@storybook/react-vite';
import { ConfirmDeleteRowsModal } from '../src/components/ConfirmDeleteRowsModal';
import { CostGateModal } from '../src/components/CostGateModal';
import { ExtractSheetNameDialog } from '../src/workbench/extract/ExtractSheetNameDialog';

const meta = { title: 'Inventory/Dialogs' } satisfies Meta;
export default meta;
type Story = StoryObj<typeof meta>;
const noop = () => undefined;

export const DeleteRows: Story = {
  render: () => <ConfirmDeleteRowsModal count={12} onConfirm={noop} onCancel={noop} />,
};
export const ExtractSheet: Story = {
  render: () => <ExtractSheetNameDialog suggestedName="Council records" onConfirm={noop} onCancel={noop} />,
};
export const CostGate: Story = {
  render: () => <CostGateModal estimate={{ cost: 2.5, billed_cost: 2_500_000, rows: 120, policy_id: 'frisket.identity.v1' }}
    message="This run exceeds your pre-approval limit. Review the estimated cost before continuing."
    onConfirm={noop} onCancel={noop} />,
};
