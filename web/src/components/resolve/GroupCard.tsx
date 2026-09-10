// One canonical group/bucket. Header: collapse chevron · ◆ · editable
// canonical-name input · total · delete. Body: member rows, each with a
// promote-◆ (make this value the canonical name) and a remove ×. Fully
// controlled: members, name, and collapsed state all live in the parent; the
// only local state is the name input's uncommitted draft.
//
// Name-edit contract: Enter confirms · Esc reverts · an empty confirmed name
// falls back to `defaultName` (the most frequent member). Blur also confirms, so
// tabbing away never silently drops a typed name.
import {
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from 'react';
import { ChevronDown, ChevronRight, Diamond, Trash2, X } from 'lucide-react';
import './resolve.css';

export interface GroupMember {
  value: string;
  count: number;
}

export function GroupCard({
  name,
  defaultName,
  members,
  onNameChange,
  onDelete,
  onRemoveMember,
  onPromoteMember,
  collapsed = false,
  onCollapsedChange,
  autoFocusName = false,
  totalCount,
  children,
  testId = 'resolve-group-card',
}: {
  /** Current canonical name (controlled). */
  name: string;
  /** Fallback committed when the name is confirmed empty — typically the
   *  group's most frequent member value. */
  defaultName: string;
  /** Member values with counts, in the parent's chosen order. */
  members: readonly GroupMember[];
  onNameChange(name: string): void;
  /** ⌫ delete-group affordance (parent returns members to unassigned). */
  onDelete(): void;
  /** × on a member row. */
  onRemoveMember(value: string): void;
  /** ◆ on a member row — parent should set `name` to this value. */
  onPromoteMember(value: string): void;
  /** Collapsed renders the ▸ summary row (name · n values · total). */
  collapsed?: boolean;
  onCollapsedChange?(collapsed: boolean): void;
  /** Focus + select the name input on mount (freshly created bucket). */
  autoFocusName?: boolean;
  /** Defaults to the sum of member counts. */
  totalCount?: number;
  /** Extra body content below the members — e.g. the inline ⌕ add-value box. */
  children?: ReactNode;
  testId?: string;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  /** Set right before a programmatic blur (Enter/Esc) so the blur handler
   *  doesn't double-commit or commit a just-reverted draft. */
  const suppressBlurCommitRef = useRef(false);
  const [draft, setDraft] = useState(name);
  const [editing, setEditing] = useState(false);

  // Resync the draft whenever the canonical name changes outside an active
  // edit (e.g. a member ◆ promotion while the input is not focused). Render-
  // time prop-change sync (the react.dev "adjusting state when a prop
  // changes" pattern) rather than an effect.
  const [syncedName, setSyncedName] = useState(name);
  if (name !== syncedName) {
    setSyncedName(name);
    if (!editing) setDraft(name);
  }

  useEffect(() => {
    if (autoFocusName && !collapsed) {
      inputRef.current?.focus();
      inputRef.current?.select();
    }
    // Mount-time affordance only — refocusing on every prop change would
    // steal focus mid-flow.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const total = totalCount ?? members.reduce((sum, member) => sum + member.count, 0);

  const commitDraft = () => {
    const trimmed = draft.trim();
    const next = trimmed === '' ? defaultName : trimmed;
    setDraft(next);
    if (next !== name) onNameChange(next);
  };

  const handleNameKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      commitDraft();
      suppressBlurCommitRef.current = true;
      inputRef.current?.blur();
    } else if (event.key === 'Escape') {
      // Revert; stop propagation so the drawer's Esc-dismiss never sees it.
      event.preventDefault();
      event.stopPropagation();
      setDraft(name);
      suppressBlurCommitRef.current = true;
      inputRef.current?.blur();
    }
  };

  const handleNameBlur = () => {
    setEditing(false);
    if (suppressBlurCommitRef.current) {
      suppressBlurCommitRef.current = false;
      return;
    }
    commitDraft();
  };

  return (
    <section
      className="resolve-group-card"
      data-testid={testId}
      data-collapsed={collapsed ? 'true' : undefined}
    >
      <div className="resolve-group-header">
        <button
          type="button"
          className="resolve-group-collapse"
          data-testid="resolve-group-collapse"
          aria-expanded={!collapsed}
          aria-label={collapsed ? `Expand group ${name}` : `Collapse group ${name}`}
          onClick={() => onCollapsedChange?.(!collapsed)}
        >
          {collapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
        </button>
        {collapsed ? (
          <span className="resolve-group-summary" data-testid="resolve-group-summary">
            <span className="resolve-group-summary-name">{name}</span>
            <span className="resolve-group-summary-meta">
              {members.length} value{members.length === 1 ? '' : 's'} ·{' '}
              {total.toLocaleString()}
            </span>
          </span>
        ) : (
          <>
            <Diamond size={12} className="resolve-group-diamond" aria-hidden />
            <input
              ref={inputRef}
              className="form-input resolve-group-name"
              data-testid="resolve-group-canonical-input"
              value={draft}
              placeholder={defaultName}
              aria-label="Canonical name"
              onChange={(event) => setDraft(event.target.value)}
              onFocus={() => setEditing(true)}
              onKeyDown={handleNameKeyDown}
              onBlur={handleNameBlur}
            />
            <span className="resolve-group-total" data-testid="resolve-group-total">
              {total.toLocaleString()}
            </span>
            <button
              type="button"
              className="icon-btn resolve-group-delete"
              data-testid="resolve-group-delete"
              aria-label={`Delete group ${name}`}
              title="Delete group and return its values"
              onClick={onDelete}
            >
              <Trash2 size={13} />
            </button>
          </>
        )}
      </div>
      {!collapsed && (
        <div className="resolve-group-members">
          {members.map((member) => {
            const isCanonical = member.value === name;
            return (
              <div
                key={member.value}
                className={`resolve-group-member${isCanonical ? ' is-canonical' : ''}`}
                data-testid="resolve-group-member"
                data-value={member.value}
              >
                <button
                  type="button"
                  className="resolve-member-promote"
                  data-testid="resolve-group-member-promote"
                  aria-label={`Use "${member.value}" as the canonical name`}
                  aria-pressed={isCanonical}
                  disabled={isCanonical}
                  title={
                    isCanonical
                      ? 'Current canonical value'
                      : `Use "${member.value}" as the canonical name`
                  }
                  onClick={() => onPromoteMember(member.value)}
                >
                  <Diamond
                    size={11}
                    aria-hidden
                    fill={isCanonical ? 'currentColor' : 'none'}
                  />
                </button>
                <span className="resolve-member-value" title={member.value}>
                  {member.value}
                </span>
                <span className="resolve-member-count">
                  {member.count.toLocaleString()}
                </span>
                <button
                  type="button"
                  className="icon-btn resolve-member-remove"
                  data-testid="resolve-group-member-remove"
                  aria-label={`Remove "${member.value}" from group`}
                  onClick={() => onRemoveMember(member.value)}
                >
                  <X size={12} />
                </button>
              </div>
            );
          })}
          {children}
        </div>
      )}
    </section>
  );
}
