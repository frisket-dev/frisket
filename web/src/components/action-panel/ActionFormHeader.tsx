import { useEffect, useRef, useState } from 'react';
import {
  ArrowLeft,
  Braces,
  CalendarClock,
  Check,
  ChevronDown,
  CircleHelp,
  Code,
  Combine,
  FileSearch,
  GitBranch,
  Globe,
  Languages,
  ListFilter,
  Mic,
  Regex,
  Scale,
  ScrollText,
  type LucideIcon,
} from 'lucide-react';

import type { ActionTemplate } from '../../api/types';
import { PanelHeader } from '../PanelPrimitives';
import { ActionHelpPopover } from './ActionHelpPopover';

const ACTION_ICONS: Partial<Record<string, LucideIcon>> = {
  'map.classify': ListFilter,
  'map.extract': FileSearch,
  'map.summarize': ScrollText,
  'map.ask': CircleHelp,
  'map.translate': Languages,
  'research.web_search': Globe,
  'media.transcribe': Mic,
  'media.ocr': FileSearch,
  'media.to_markdown': ScrollText,
  'web.capture_page': Globe,
  'derive.table_from_list': GitBranch,
  'media.extract_pdf_tables': FileSearch,
  'join.semantic': Combine,
  'map.python': Code,
  'map.regex_extract': Regex,
  'reduce.group_summary': Combine,
  'map.judge': Scale,
  'enrich.geocode': Globe,
  'enrich.census_demographics': Globe,
};

const CATEGORY_ICONS: Record<string, LucideIcon> = {
  cleanup: CalendarClock,
  text: Braces,
};

export interface ActionFormHeaderProps {
  actionTemplate: ActionTemplate;
  title?: string;
  sheetName?: string;
  switchActions?: ActionTemplate[];
  onSwitchAction?(actionTemplate: ActionTemplate): void;
  variant: 'panel' | 'drawer';
  onBack?: () => void;
  onClose?: () => void;
}

export function ActionFormHeader({
  actionTemplate,
  title,
  sheetName,
  switchActions,
  onSwitchAction,
  variant,
  onBack,
  onClose,
}: ActionFormHeaderProps) {
  const [actionMenuOpen, setActionMenuOpen] = useState(false);
  const actionMenuRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!actionMenuOpen) return;
    const closeIfOutside = (event: PointerEvent | FocusEvent) => {
      const target = event.target;
      if (target instanceof Node && actionMenuRef.current?.contains(target)) return;
      setActionMenuOpen(false);
    };
    document.addEventListener('pointerdown', closeIfOutside);
    document.addEventListener('focusin', closeIfOutside);
    return () => {
      document.removeEventListener('pointerdown', closeIfOutside);
      document.removeEventListener('focusin', closeIfOutside);
    };
  }, [actionMenuOpen]);

  const formTitle = title ?? actionTemplate.name;
  const HeaderIcon = ACTION_ICONS[actionTemplate.kind]
    ?? (actionTemplate.actionCategory ? CATEGORY_ICONS[actionTemplate.actionCategory] : undefined)
    ?? ListFilter;
  const canSwitchActions = Boolean(
    switchActions && switchActions.length > 0 && onSwitchAction,
  );

  return (
    <PanelHeader
      className={`panel-header form-header${variant === 'drawer' ? ' action-drawer-header' : ''}`}
      kicker={variant === 'drawer' ? (
        <span className="action-drawer-header-icon" aria-hidden>
          <HeaderIcon size={16} />
        </span>
      ) : (
          <button type="button" className="icon-btn" onClick={onBack} aria-label="Back to actions">
          <ArrowLeft size={15} />
        </button>
      )}
      title={canSwitchActions ? (
        <div className="action-title-menu" ref={actionMenuRef}>
          <button
            type="button"
            className="action-title-button"
            data-testid="action-title-menu-button"
            aria-haspopup="menu"
            aria-expanded={actionMenuOpen}
            onClick={() => setActionMenuOpen((open) => !open)}
          >
            <span className="action-title-text" data-testid="action-form-title">{formTitle}</span>
            <ChevronDown size={14} className="action-title-caret" aria-hidden />
          </button>
          {actionMenuOpen && (
            <div className="action-title-menu-list" role="menu" data-testid="action-title-menu">
              <div className="action-title-menu-label">Change action</div>
              {switchActions?.map((candidate) => (
                <button
                  key={candidate.kind}
                  type="button"
                  role="menuitem"
                  className="action-title-menu-item"
                  data-testid={`action-switch-${candidate.kind}`}
                  onClick={() => {
                    setActionMenuOpen(false);
                    if (candidate.kind !== actionTemplate.kind) onSwitchAction?.(candidate);
                  }}
                >
                  <span>
                    <span className="action-name">
                      {candidate.name}
                      {candidate.kind === actionTemplate.kind && <Check size={12} aria-hidden />}
                    </span>
                    <span className="action-desc">{candidate.description}</span>
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>
      ) : (
        <span data-testid="action-form-title">{formTitle}</span>
      )}
      chips={sheetName ? (
        <span className="muted action-form-context" title={`on ${sheetName}`}>
          on {sheetName}
        </span>
      ) : undefined}
      actions={variant === 'drawer' ? <ActionHelpPopover action={actionTemplate} /> : undefined}
      onClose={variant === 'drawer' ? onClose : undefined}
      closeTestId="action-drawer-close"
      closeClassName="icon-btn panel-frame-close action-drawer-close"
      closeAriaLabel="Close action drawer"
      closeIconSize={16}
    />
  );
}
