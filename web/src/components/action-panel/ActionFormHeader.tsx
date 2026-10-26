import '../../workbench/ActionDrawer.css';
import '../ActionPanel.css';
import { useRef, useState } from 'react';
import {
  ArrowLeft,
  Braces,
  CalendarClock,
  Check,
  CircleHelp,
  Code,
  Combine,
  FileSearch,
  FunctionSquare,
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
import { DropdownIndicator } from '../DropdownIndicator';
import { IconButton } from '../IconButton';
import { AnchoredMenu } from '../MenuPop';
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
  'map.formula': FunctionSquare,
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
  /** Visible launch provenance, e.g. `on Court filings · from variant B`. */
  contextLabel?: string;
  onOpenHistory?(trigger: HTMLButtonElement): void;
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
  contextLabel,
  onOpenHistory,
  switchActions,
  onSwitchAction,
  variant,
  onBack,
  onClose,
}: ActionFormHeaderProps) {
  const [actionMenuOpen, setActionMenuOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement | null>(null);


  const formTitle = title ?? actionTemplate.name;
  const HeaderIcon = ACTION_ICONS[actionTemplate.kind]
    ?? (actionTemplate.actionCategory ? CATEGORY_ICONS[actionTemplate.actionCategory] : undefined)
    ?? ListFilter;
  const canSwitchActions = Boolean(
    switchActions && switchActions.length > 0 && onSwitchAction,
  );

  return (
    <PanelHeader
      className={`panel-header form-header${variant === 'drawer' ? ' action-drawer-header' : ''}${
        variant === 'drawer' && onOpenHistory ? ' action-drawer-header-with-history' : ''}`}
      kicker={variant === 'drawer' ? (
        <span className="action-drawer-header-icon" aria-hidden>
          <HeaderIcon size={16} />
        </span>
      ) : (
        <IconButton icon={ArrowLeft} onClick={onBack} label="Back to actions" />
      )}
      title={canSwitchActions ? (
        <div className="action-title-menu">
          <button
            type="button"
            className="action-title-button"
            ref={triggerRef}
            data-testid="action-title-menu-button"
            aria-haspopup="menu"
            aria-expanded={actionMenuOpen}
            onClick={() => setActionMenuOpen((open) => !open)}
          >
            <span className="action-title-text" data-testid="action-form-title">{formTitle}</span>
            <DropdownIndicator />
          </button>
          {actionMenuOpen && (
            <AnchoredMenu anchorRef={triggerRef} onClose={() => setActionMenuOpen(false)} width={280}
              dismissOnFocusOutside data-testid="action-title-menu">
              <div className="menu-label">Change action</div>
              {switchActions?.map((candidate) => (
                <button
                  key={candidate.kind}
                  type="button"
                  role="menuitem"
                  className="menu-item"
                  data-testid={`action-switch-${candidate.kind}`}
                  onClick={() => {
                    setActionMenuOpen(false);
                    if (candidate.kind !== actionTemplate.kind) onSwitchAction?.(candidate);
                  }}
                >
                  <span className="menu-item-copy">
                    <span className="menu-item-label">
                      {candidate.name}
                      {candidate.kind === actionTemplate.kind && <Check size={12} aria-hidden />}
                    </span>
                    <span className="menu-item-description">{candidate.description}</span>
                  </span>
                </button>
              ))}
            </AnchoredMenu>
          )}
        </div>
      ) : (
        <span data-testid="action-form-title">{formTitle}</span>
      )}
      chips={contextLabel || sheetName ? (
        <span className="muted action-form-context" title={contextLabel ?? `on ${sheetName}`}>
          {contextLabel ?? `on ${sheetName}`}
        </span>
      ) : undefined}
      actions={variant === 'drawer' ? <>
        {onOpenHistory && <IconButton icon={CalendarClock}
          onClick={(event) => onOpenHistory(event.currentTarget)}
          className="action-history-trigger" label="Earlier runs" data-testid="action-history-button" />}
        <ActionHelpPopover action={actionTemplate} />
      </> : undefined}
      onClose={variant === 'drawer' ? onClose : undefined}
      closeTestId="action-drawer-close"
      closeClassName="action-drawer-close"
      closeAriaLabel="Close action drawer"
    />
  );
}
