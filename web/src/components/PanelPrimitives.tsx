import { useId, type InputHTMLAttributes, type ReactNode } from 'react';
import { X, type LucideIcon } from 'lucide-react';

/**
 * Shared action-panel form primitives: a full-width segmented switch and a
 * titled toggle-row card. Both
 * keep a real focusable control in the DOM so keyboard and e2e access work.
 *
 * Component exports only (react-refresh/only-export-components) — plain
 * helper functions shared between panels (testIdKey, panelErrorMessage) live
 * in ./panelPrimitivesModel instead.
 */

/**
 * The one muted "nothing here" block. Replaces the
 * literal-duplicate -empty classes (history/sources/notifications/watches …)
 * so every region's empty state renders with the SAME font, color, and padding
 * regardless of its host (dock vs Discover). Contextual/action-prompt empties
 * that carry their own layout stay bespoke; this is the plain muted line.
 */
export function PanelEmpty({
  children,
  className,
  testId,
  role,
  ...rest
}: {
  children: ReactNode;
  className?: string;
  testId?: string;
  role?: string;
} & Record<`data-${string}`, string | undefined>) {
  const classes = ['panel-empty', className].filter(Boolean).join(' ');
  return (
    <div className={classes} data-testid={testId} role={role} {...rest}>
      {children}
    </div>
  );
}

/**
 * The one muted "please wait" placeholder. Replaces app-loading/drawer-loading/evidence-viewer-
 * loading/action-drawer-loading (four re-declared "muted text, no icon"
 * blocks) so every plain loading placeholder — full-page routes, Suspense
 * fallbacks, drawer sections — renders with the SAME font, color, and
 * padding regardless of host, the same convergence PanelEmpty did for empty
 * states. Deliberately hook-free: it is used directly as a Suspense
 * `fallback` element in several call sites, and React forbids a suspending
 * fallback tree from itself depending on hooks/state. Icon-bearing
 * (receipt-loading), inline-ellipsis (home-card-meta-loading), and
 * host-fill-with-plugin-attrs (trusted-local-plugin-component-loading)
 * shapes are a different genus and are NOT this primitive.
 */
export function PanelLoading({
  label,
  className,
  testId,
  role = 'status',
  ...rest
}: {
  label: ReactNode;
  /** Extra layout class composed alongside `panel-loading` (e.g. `grid-host`
   *  for pane-filling Suspense fallbacks, `panel-loading-page` for full-height
   *  centered page/route states). */
  className?: string;
  testId?: string;
  role?: string;
} & Record<`data-${string}`, string | undefined>) {
  const classes = ['panel-loading', className].filter(Boolean).join(' ');
  return (
    <div className={classes} data-testid={testId} role={role} {...rest}>
      {label}
    </div>
  );
}

/**
 * The one inline error block. Replaces the
 * byte-identical -error blocks (sources-error ≡ watches-error). role="alert"
 * by default so assistive tech and the existing specs keep working.
 */
export function PanelError({
  children,
  className,
  testId,
  role = 'alert',
  ...rest
}: {
  children: ReactNode;
  className?: string;
  testId?: string;
  role?: string;
} & Record<`data-${string}`, string | undefined>) {
  const classes = ['panel-error', className].filter(Boolean).join(' ');
  return (
    <div className={classes} data-testid={testId} role={role} {...rest}>
      {children}
    </div>
  );
}

/**
 * The one status/state chip. Replaces four
 * re-declared tone->color maps (notification-status-pill, ollama-badge,
 * embedding-badge, sheet-info-badge, and LineagePanel's lineage-node-sync)
 * with a single primitive: tone maps to
 * color in exactly ONE place — the `.status-chip[data-tone=...]` rules in
 * styles.css. Count/removable chips (multi-col-chip, engine-tier-chip,
 * ocr-compare-vote-chip) are a different genus and are NOT this primitive.
 */
export type StatusTone = 'success' | 'warning' | 'error' | 'info' | 'neutral';

export function StatusChip({
  tone,
  size = 'md',
  uppercase = false,
  children,
  testId,
  className,
  title,
  ...rest
}: {
  tone: StatusTone;
  /** sm = the compact inline pills (delivery status, index freshness); md =
   *  the larger standalone badges (sheet sync, provider reachability). */
  size?: 'sm' | 'md';
  /** Uppercases the rendered text (the two sites that read as all-caps
   *  labels); the DOM text itself stays whatever the caller passes so it
   *  remains assertable in lowercase. */
  uppercase?: boolean;
  children: ReactNode;
  testId?: string;
  className?: string;
  /** Native tooltip (EngineTierBadge's per-tier explanation). */
  title?: string;
} & Record<`data-${string}`, string | undefined>) {
  const classes = ['status-chip', className].filter(Boolean).join(' ');
  return (
    <span
      className={classes}
      data-tone={tone}
      data-size={size}
      data-caps={uppercase ? 'true' : undefined}
      data-testid={testId}
      title={title}
      {...rest}
    >
      {children}
    </span>
  );
}

export interface SegmentedOption {
  value: string;
  label: string;
  /** Optional leading icon (view switchers etc.). */
  icon?: LucideIcon;
  /** When set, the segment renders VISIBLE but disabled, naming why via
   *  title + data-disabled-reason instead of silently omitting it (a hidden
   *  map with geo data present was an unexplainable mystery). Callers that want a segment omitted entirely should filter
   *  it out of `options` themselves. */
  disabledReason?: string;
}

export function SegmentedToggle({
  options,
  value,
  onValueChange,
  ariaLabel,
  fullWidth = true,
  buttonTestId,
  selectTestId,
  testId,
  className,
}: {
  options: SegmentedOption[];
  value: string;
  onValueChange(value: string): void;
  ariaLabel?: string;
  fullWidth?: boolean;
  /** Per-option testid for the visible buttons. */
  buttonTestId?: (value: string) => string;
  /** When set, mirrors state into a visually-hidden native select carrying
   *  this testid, so selectOption()/toHaveValue() keep working. */
  selectTestId?: string;
  /** Testid for the outer group wrapper. */
  testId?: string;
  className?: string;
}) {
  const classes = ['segmented', fullWidth ? 'segmented-full' : '', className ?? '']
    .filter(Boolean)
    .join(' ');
  return (
    <div className={classes} role="group" aria-label={ariaLabel} data-testid={testId}>
      {options.map((option) => {
        const disabled = Boolean(option.disabledReason);
        const Icon = option.icon;
        return (
          <button
            key={option.value}
            type="button"
            className={[
              value === option.value ? 'active' : '',
              disabled ? 'segmented-option-disabled' : '',
            ]
              .filter(Boolean)
              .join(' ')}
            data-testid={buttonTestId ? buttonTestId(option.value) : undefined}
            aria-pressed={value === option.value}
            disabled={disabled}
            data-disabled-reason={option.disabledReason}
            title={option.disabledReason}
            onClick={() => onValueChange(option.value)}
          >
            {Icon && <Icon size={13} aria-hidden />}
            {option.label}
          </button>
        );
      })}
      {selectTestId && (
        <select data-native-select-escape="segmented-toggle-mirror"
          className="sr-only-select"
          data-testid={selectTestId}
          aria-label={ariaLabel}
          tabIndex={-1}
          value={value}
          onChange={(event) => onValueChange(event.target.value)}
        >
          {options.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      )}
    </div>
  );
}

export function ToggleRow({
  title,
  description,
  checked,
  onCheckedChange,
  testId,
  disabled,
  tag,
}: {
  title: string;
  description?: string;
  checked: boolean;
  onCheckedChange(checked: boolean): void;
  testId?: string;
  /** Renders the row muted and locks the switch (e.g. a required setting
   *  the backend never accepts as a toggle). */
  disabled?: boolean;
  /** Small muted qualifier next to the title, e.g. "required". */
  tag?: string;
}) {
  const inputId = useId();
  return (
    <label
      className={`toggle-row${disabled ? ' toggle-row-disabled' : ''}`}
      htmlFor={inputId}
    >
      <span className="toggle-row-text">
        <span className="toggle-row-title">
          {title}
          {tag && <span className="toggle-row-tag">{tag}</span>}
        </span>
        {description && <span className="toggle-row-desc">{description}</span>}
      </span>
      <span className="toggle-row-switch-wrap">
        <input
          id={inputId}
          type="checkbox"
          className="toggle-row-input"
          data-testid={testId}
          checked={checked}
          disabled={disabled}
          onChange={(event) => onCheckedChange(event.target.checked)}
        />
        <span className="toggle-row-switch" aria-hidden />
      </span>
    </label>
  );
}

/**
 * The one panel-header anatomy. An earlier pass shipped only the CSS half — the shared
 * `.panel-frame-header` / `.panel-frame-close` rules four of the six
 * anatomies already carry as classNames. This is the COMPONENT half: a
 * structural kicker/title/chips/actions/close slot order, with CSS left
 * entirely to the caller's `className` (some sites, e.g. ActionPanel's
 * action-drawer-header, deliberately never adopted `.panel-frame-header` —
 * migrating the component must not retrofit that CSS decision). The close
 * chip renders only when `onClose` is passed (LineagePanel/DocumentReader
 * have no dismiss affordance at all), and its className/aria-label/icon size
 * are all caller-overridable because the six anatomies genuinely differ
 * (Drawer's "Close drawer" vs ActionPanel's "Close action drawer" + 16px
 * icon vs everyone else's 15px).
 */
export function PanelHeader({
  kicker,
  title,
  chips,
  actions,
  onClose,
  closeTestId,
  closeAriaLabel = 'Close',
  closeTitle,
  closeClassName = 'icon-btn panel-frame-close',
  closeIconSize = 15,
  className,
  testId,
}: {
  kicker?: ReactNode;
  title?: ReactNode;
  chips?: ReactNode;
  actions?: ReactNode;
  onClose?: () => void;
  closeTestId?: string;
  closeAriaLabel?: string;
  closeTitle?: string;
  closeClassName?: string;
  closeIconSize?: number;
  className?: string;
  testId?: string;
}) {
  return (
    <header className={className} data-testid={testId}>
      {kicker}
      {title}
      {chips}
      {actions}
      {onClose && (
        <button
          type="button"
          className={closeClassName}
          data-testid={closeTestId}
          aria-label={closeAriaLabel}
          title={closeTitle}
          onClick={onClose}
        >
          <X size={closeIconSize} />
        </button>
      )}
    </header>
  );
}

export interface ConfirmTypeInputProps
  extends Omit<InputHTMLAttributes<HTMLInputElement>, 'value' | 'onChange' | 'autoComplete' | 'type'> {
  value: string;
  onChange(value: string): void;
}

/**
 * A "type X to confirm" danger-zone/gate input (project delete, project
 * compaction, the billable-run cost gate, …). The typed word is never a
 * credential, but a plain text input still reads as one to a browser's
 * form-history autosuggest — clicking the field offers a saved-value
 * dropdown from a prior fill. autoComplete="off" opts every caller out
 * generically (and no `name` attribute, the default here, is the only other
 * thing that would let form-history key on this field) instead of each
 * danger-zone form remembering to set it itself.
 */
export function ConfirmTypeInput({ value, onChange, ...rest }: ConfirmTypeInputProps) {
  return (
    <input
      {...rest}
      autoComplete="off"
      value={value}
      onChange={(event) => onChange(event.currentTarget.value)}
    />
  );
}
