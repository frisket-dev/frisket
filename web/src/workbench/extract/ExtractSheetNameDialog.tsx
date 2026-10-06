import { useEffect, useRef, useState } from 'react';

export function ExtractSheetNameDialog({ suggestedName, onCancel, onConfirm }: {
  suggestedName: string;
  onCancel(): void;
  onConfirm(name: string): void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [name, setName] = useState(suggestedName);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) {
      if (typeof dialog.showModal === 'function') dialog.showModal();
      else dialog.setAttribute('open', '');
    }
  }, []);

  return <dialog ref={dialogRef} className="modal-card" aria-labelledby="extract-sheet-name-title"
    onCancel={(event) => { event.preventDefault(); onCancel(); }}>
    <form onSubmit={(event) => { event.preventDefault(); if (name.trim()) onConfirm(name.trim()); }}>
      <div className="modal-title" id="extract-sheet-name-title">Extract to new sheet</div>
      <label>Sheet name <input autoFocus className="form-input" aria-label="Result sheet name" value={name}
        onChange={(event) => setName(event.target.value)} /></label>
      <div className="form-actions">
        <button type="button" className="btn" onClick={onCancel}>Cancel</button>
        <button type="submit" className="btn btn-primary" disabled={!name.trim()}>Extract</button>
      </div>
    </form>
  </dialog>;
}
