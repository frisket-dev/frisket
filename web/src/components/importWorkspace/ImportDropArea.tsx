import { useState, type ReactNode } from 'react';
import { Upload } from 'lucide-react';
import './ImportWorkspaceShared.css';

/** Shared file-drop presentation; each importer still owns selection and processing. */
export function ImportDropArea({ title, description, disabled = false, onFiles, children, testId }: {
  title: string;
  description: ReactNode;
  disabled?: boolean;
  onFiles(files: File[]): void;
  children: ReactNode;
  testId?: string;
}) {
  const [dragging, setDragging] = useState(false);
  return <div className="import-drop-area" data-testid={testId}
    data-dragging={dragging && !disabled || undefined}
    onDragOver={(event) => {
      event.preventDefault();
      event.stopPropagation();
      event.dataTransfer.dropEffect = disabled ? 'none' : 'copy';
      if (!disabled) setDragging(true);
    }}
    onDragLeave={(event) => {
      if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false);
    }}
    onDrop={(event) => {
      event.preventDefault();
      event.stopPropagation();
      setDragging(false);
      const files = Array.from(event.dataTransfer.files);
      if (!disabled && files.length) onFiles(files);
    }}>
    <Upload size={32} aria-hidden />
    <h3>{title}</h3>
    <p>{description}</p>
    {children}
  </div>;
}
