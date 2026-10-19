import { Button } from '../../components/Button';
import { TextInput } from '../../components/FormControls';
import { useState } from 'react';
import { Modal } from '../../components/Modal';

export function ExtractSheetNameDialog({ suggestedName, onCancel, onConfirm }: {
  suggestedName: string;
  onCancel(): void;
  onConfirm(name: string): void;
}) {
  const [name, setName] = useState(suggestedName);


  return <Modal title="Extract to new sheet" onClose={onCancel}>
    <form onSubmit={(event) => { event.preventDefault(); if (name.trim()) onConfirm(name.trim()); }}>
      <label className="form-field">Sheet name <TextInput autoFocus  aria-label="Result sheet name" value={name}
        onChange={(event) => setName(event.target.value)} /></label>
      <div className="form-actions">
        <Button type="button" onClick={onCancel}>Cancel</Button>
        <Button type="submit" className="btn-primary" disabled={!name.trim()}>Extract</Button>
      </div>
    </form>
  </Modal>;
}
