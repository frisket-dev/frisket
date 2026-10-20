import '../FormControls.css';
import './RunScopeFooter.css';
import { Button } from '../Button';
import { useId, useRef, useState, type ReactNode } from 'react';
import { Eye, Play, RefreshCcw } from 'lucide-react';
import { DropdownIndicator } from '../DropdownIndicator';
import { AnchoredMenu } from '../MenuPop';

export interface RunScopeFooterProps {
  children?: ReactNode;
  primaryLabel: string;
  previewLabel?: string;
  rowCount: number;
  selectedCount: number;
  scope?: 'all' | 'selected';
  onScopeChange?(scope: 'all' | 'selected'): void;
  /** Existing callers launch immediately from the split-button menu. A
   * priced generated form can instead make the menu a scope selector so its
   * newly scoped estimate is visible before Run is pressed. */
  runOnScopeSelect?: boolean;
  /** Omitted keeps the ordinary all/selected choice. Catalog-generated forms
   * pass their server-owned row-scope policy directly. */
  allowedScopes?: readonly ('all_rows' | 'exact_membership')[];
  /** An exact view/lens may intentionally contain zero rows. */
  allowEmptySelection?: boolean;
  primaryButtonType?: 'button' | 'submit';
  primaryTestId?: string;
  backfillTargetName?: string | null;
  backfillHasGaps?: boolean;
  onBackfill?(targetColumnName: string): void;
  canRun: boolean;
  canPreview?: boolean;
  running?: boolean;
  runningLabel?: string;
  disabledReason?: string;
  previewDisabledReason?: string;
  testIdPrefix: string;
  onPreview(scope: 'all' | 'selected'): void;
  onRun(scope: 'all' | 'selected'): void;
}

export function RunScopeFooter({
  children,
  primaryLabel,
  previewLabel = 'Preview',
  rowCount,
  selectedCount,
  scope: controlledScope,
  onScopeChange,
  runOnScopeSelect = true,
  allowedScopes = ['all_rows', 'exact_membership'],
  allowEmptySelection = false,
  primaryButtonType = 'button',
  primaryTestId,
  backfillTargetName = null,
  backfillHasGaps = false,
  onBackfill,
  canRun,
  canPreview = canRun,
  running = false,
  runningLabel = 'Running…',
  disabledReason,
  previewDisabledReason,
  testIdPrefix,
  onPreview,
  onRun,
}: RunScopeFooterProps) {
  const allAllowed = allowedScopes.includes('all_rows');
  const selectedAllowed = allowedScopes.includes('exact_membership');
  const [localScope, setLocalScope] = useState<'all' | 'selected'>(
    selectedAllowed && selectedCount > 0 ? 'selected' : 'all',
  );
  const [menuOpen, setMenuOpen] = useState(false);
  const toggleRef = useRef<HTMLButtonElement | null>(null);
  const scopePopupId = useId();


  // Clamp at render and invocation time. No state-sync effect is needed: when
  // selection is empty, neither the summary nor either callback can observe a
  // stale selected scope.
  const requestedScope = controlledScope ?? localScope;
  const effectiveScope = requestedScope === 'selected' && selectedAllowed
    && (selectedCount > 0 || allowEmptySelection)
    ? 'selected'
    : allAllowed
      ? 'all'
      : 'selected';
  const scopeAvailable = effectiveScope === 'all'
    ? allAllowed
    : selectedAllowed && (selectedCount > 0 || allowEmptySelection);
  const hasScopeChoice = allAllowed && selectedAllowed;
  const hasScopeMenu = hasScopeChoice || Boolean(onBackfill);
  const canBackfill = Boolean(onBackfill && backfillTargetName && backfillHasGaps);
  const backfillTitle = !backfillTargetName
    ? 'Select an existing AI column to re-run its failed or missing rows'
    : !backfillHasGaps
      ? 'No failed or missing rows in this column'
      : `Re-run missing cells in ${backfillTargetName}`;

  const runScope = (next: 'all' | 'selected') => {
    setLocalScope(next);
    onScopeChange?.(next);
    if (runOnScopeSelect) onRun(next);
    setMenuOpen(false);
    toggleRef.current?.focus();
  };


  const disabledTitle = !canRun
    ? disabledReason ?? 'Complete the required fields.'
    : !scopeAvailable
      ? effectiveScope === 'selected' && selectedCount === 0
        ? 'No rows selected.'
        : 'No valid row scope is available.'
      : undefined;
  const visiblePrimaryLabel = running
    ? (canRun ? 'Queue' : runningLabel)
    : primaryLabel;

  return (
    <div className="form-actions action-run-actions">
      {children}
      <p className="run-scope-summary">
        {effectiveScope === 'all'
          ? `All ${rowCount.toLocaleString()} rows will run`
          : `${selectedCount.toLocaleString()} selected rows will run`}
      </p>
      <div className="run-actions-row">
        <Button
          type="button"

          disabled={!canPreview || !scopeAvailable}
          title={!canPreview ? previewDisabledReason ?? disabledTitle : disabledTitle}
          data-testid={`${testIdPrefix}-preview`}
          onClick={() => onPreview(effectiveScope)}
        >
          <Eye size={13} /> {previewLabel}
        </Button>
          <div className="run-scope-control">
            <div className="run-scope-button-row">
            <Button
              type={primaryButtonType}
              className="btn-primary run-primary"
              disabled={!canRun || !scopeAvailable}
              title={disabledTitle}
              data-testid={primaryTestId ?? `${testIdPrefix}-run`}
              onClick={primaryButtonType === 'button' ? () => onRun(effectiveScope) : undefined}
            >
              <Play size={13} /> {visiblePrimaryLabel}
            </Button>
            {hasScopeMenu && <Button
              ref={toggleRef}
              type="button"
              className="btn-primary run-scope-toggle"
              aria-label="Choose run scope"
              aria-expanded={menuOpen}
              aria-controls={scopePopupId}
              disabled={!canRun || (!scopeAvailable && !hasScopeChoice)}
              title={disabledTitle}
              data-testid={`${testIdPrefix}-run-scope-menu-button`}
              onClick={() => setMenuOpen((open) => !open)}
            >
              <DropdownIndicator className="run-scope-indicator" />
            </Button>}
          </div>
          {hasScopeMenu && menuOpen && (
            <AnchoredMenu
              anchorRef={toggleRef}
              onClose={() => setMenuOpen(false)}
              align="right"
              id={scopePopupId}
              role="group"
              aria-label="Run scope options"
              data-testid={`${testIdPrefix}-run-scope-menu`}
            >
              {hasScopeChoice && <button
                type="button"
                className="menu-item"
                data-active={effectiveScope === 'all'}
                data-testid={`${testIdPrefix}-row-scope-all`}
                onClick={() => runScope('all')}
              >
                <span className="menu-item-name">Run all</span>
                <span className="muted">{rowCount.toLocaleString()} rows</span>
              </button>}
              {hasScopeChoice && <button
                type="button"
                className="menu-item"
                data-active={effectiveScope === 'selected'}
                data-testid={`${testIdPrefix}-row-scope-selected`}
                disabled={selectedCount === 0}
                onClick={() => runScope('selected')}
              >
                <span className="menu-item-name">Run on selected</span>
                <span className="muted">
                  {selectedCount > 0 ? `${selectedCount.toLocaleString()} selected` : 'No rows selected'}
                </span>
              </button>}
              {onBackfill && (
                <button
                  type="button"
                  role="menuitem"
                  className="menu-item"
                  data-testid={`${testIdPrefix}-row-scope-backfill`}
                  disabled={!canBackfill}
                  title={backfillTitle}
                  onClick={() => {
                    if (backfillTargetName && canBackfill) onBackfill(backfillTargetName);
                    setMenuOpen(false);
                    toggleRef.current?.focus();
                  }}
                >
                  <span className="menu-item-label menu-item-name"><RefreshCcw size={12} aria-hidden /> Re-run failed/missing rows</span>
                  <span className="muted">
                    {backfillTargetName
                      ? backfillHasGaps
                        ? `Missing cells in ${backfillTargetName}`
                        : 'No failed or missing rows'
                      : 'Target an existing AI column'}
                  </span>
                </button>
              )}
            </AnchoredMenu>
          )}
        </div>
      </div>
    </div>
  );
}
