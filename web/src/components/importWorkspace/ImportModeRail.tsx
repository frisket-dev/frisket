import type { ImportMode, ImportModeOption } from './model';

interface ImportModeRailProps {
  modes: ImportModeOption[];
  mode: ImportMode;
  onSelectMode(mode: ImportMode): void;
}

export function ImportModeRail({ modes, mode, onSelectMode }: ImportModeRailProps) {
  return (
    <div
      className="import-mode-list import-workspace-mode-list"
      role="tablist"
      aria-label="Import type"
    >
      {modes.map((m) => (
        <button
          key={m.id}
          type="button"
          role="tab"
          aria-selected={mode === m.id}
          className={`import-mode${mode === m.id ? ' import-mode-active' : ''}`}
          data-testid={`import-mode-${m.id}`}
          onClick={() => onSelectMode(m.id)}
        >
          <span>{m.label}</span>
          <small>{m.hint}</small>
        </button>
      ))}
    </div>
  );
}
