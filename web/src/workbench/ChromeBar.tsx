// Global chrome bar: the page-wide top strip carrying the app mark + project
// switcher, a spacer, the Command palette pill, the Copilot toggle, and an
// account affordance. It is the single home for the ProjectMenu — the sidebar no
// longer renders it.

import { BookOpen, Bot, ChevronRight, Search } from 'lucide-react';
import type { ReactNode } from 'react';
import type {
  ProjectInfo,
  SheetViewExportOptions,
  SheetMeta,
} from '../api/open';
import type { ProjectApiPort } from '../api/ports';
import type { ExportTarget } from '../exportTargets';
import { AccountMenu } from '../components/AccountMenu';
import { BrandLink } from '../components/BrandLink';
import { ProjectMenu } from '../components/TopNav';
import { useShellIdentity } from '../shellIdentity';

/** The shared chrome-bar FRAME (one height, one background, one brand-tile
 *  treatment) — both the workspace chrome and the Home screen's top bar render
 *  inside it, so the two can never drift apart again (the Home bar had grown
 *  its own 52px/--bg variant with a differently-colored mark). Content
 *  differs per surface; the wrapper is the contract. */
export function ChromeBarShell({
  brandExtra,
  children,
}: {
  brandExtra?: ReactNode;
  children: ReactNode;
}) {
  return (
    <header className="chrome-bar" data-testid="chrome-bar">
      <div className="chrome-bar-brand">
        <BrandLink />
        {brandExtra}
      </div>
      <div className="chrome-bar-spacer" />
      {children}
    </header>
  );
}

export function ChromeBar({
  project,
  projectApi,
  currentSheet,
  sheets,
  currentSheetExportOptions,
  catalogExportTargets,
  onOpenCommandPalette,
  copilotOpen,
  onToggleCopilot,
  walkthroughActive,
  walkthroughCanResume,
  walkthroughGuideSeen,
  onOpenWalkthrough,
  onResumeWalkthrough,
}: {
  project: ProjectInfo;
  projectApi: ProjectApiPort;
  currentSheet?: SheetMeta | null;
  sheets: SheetMeta[];
  currentSheetExportOptions?: SheetViewExportOptions | null;
  catalogExportTargets: ExportTarget[];
  onOpenCommandPalette(): void;
  copilotOpen: boolean;
  onToggleCopilot(): void;
  walkthroughActive: boolean;
  walkthroughCanResume: boolean;
  walkthroughGuideSeen: boolean;
  onOpenWalkthrough(): void;
  onResumeWalkthrough(): void;
}) {
  const isMac =
    typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform);
  const { identityMode, me } = useShellIdentity();
  return (
    <ChromeBarShell
      brandExtra={
        <ProjectMenu
          project={project}
          projectApi={projectApi}
          currentSheet={currentSheet}
          sheets={sheets}
          currentSheetExportOptions={currentSheetExportOptions}
          catalogExportTargets={catalogExportTargets}
        />
      }
    >
      <button
        type="button"
        className={`chrome-guide-btn${walkthroughActive ? ' open' : ''}${walkthroughGuideSeen ? '' : ' new'}`}
        data-testid="chrome-walkthrough"
        aria-pressed={walkthroughActive}
        title="Guided walkthrough"
        onClick={onOpenWalkthrough}
      >
        <BookOpen size={14} />
        <span>Guide</span>
      </button>
      {walkthroughCanResume && (
        <button
          type="button"
          className="chrome-guide-resume"
          data-testid="chrome-walkthrough-resume"
          title="Resume walkthrough"
          onClick={onResumeWalkthrough}
        >
          <span>Resume</span>
          <ChevronRight size={13} />
        </button>
      )}
      <button
        type="button"
        className="chrome-command-pill"
        data-testid="chrome-command-pill"
        onClick={onOpenCommandPalette}
      >
        <Search size={13} />
        <span className="chrome-command-label">Command</span>
        <span className="chrome-command-kbd">{isMac ? '⌘K' : 'Ctrl K'}</span>
      </button>
      <button
        type="button"
        className={`chrome-copilot-btn${copilotOpen ? ' open' : ''}`}
        data-testid="chrome-copilot-toggle"
        aria-pressed={copilotOpen}
        title="Copilot"
        onClick={onToggleCopilot}
      >
        <Bot size={15} />
      </button>
      <AccountMenu
        triggerTestId="chrome-account"
        project={project}
        identityMode={identityMode}
        me={me}
        onOpenCommandPalette={onOpenCommandPalette}
      />
    </ChromeBarShell>
  );
}
