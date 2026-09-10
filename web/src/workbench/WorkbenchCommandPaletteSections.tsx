// The command palette's "production work" sections (BEST MATCH / ACTIONS / GO
// TO) and the click-only COMMANDS section. Purely presentational: keyboard-nav
// state and every handler stay in the parent and are threaded in — DOM/classes/
// testids and the flat active-row semantics are relied on by tests, so keep them
// stable.
import { CornerDownLeft } from 'lucide-react';
import { WorkbenchCommandButtonFrame } from './contributions';
import type { WorkbenchCommandEntry } from './commandRegistry';
import { contributionSlug, type WorkbenchVisibilityTarget } from './visibility';
import type {
  PaletteActionItem,
  PaletteGotoItem,
  PaletteLauncherCommand,
} from './WorkbenchCommandPalette';

function visibilityCommandLabel(
  action: 'hide' | 'reveal',
  target: WorkbenchVisibilityTarget,
): string {
  const prefix = action === 'hide' ? 'Hide' : 'Show';
  return `${prefix} ${target.locationLabel}: ${target.title}`;
}

/** BEST MATCH / ACTIONS / GO TO — the flat keyboard-navigable production rows. */
export function PaletteProductionSections({
  filteredBestMatch,
  filteredActions,
  filteredGoto,
  activeIndex,
  actionRowIndex,
  gotoRowIndex,
  onHover,
  onLaunchAction,
  onNavigateSheet,
}: {
  filteredBestMatch: PaletteActionItem[];
  filteredActions: PaletteActionItem[];
  filteredGoto: PaletteGotoItem[];
  activeIndex: number;
  actionRowIndex(item: PaletteActionItem): number;
  gotoRowIndex(item: PaletteGotoItem): number;
  onHover(index: number): void;
  onLaunchAction(item: PaletteActionItem): void;
  onNavigateSheet(item: PaletteGotoItem): void;
}) {
  return (
    <>
      {filteredBestMatch.length > 0 && (
        <div className="palette-section" data-testid="palette-section-best-match">
          <div className="palette-section-label">Best match</div>
          {filteredBestMatch.map((item, i) => {
            const idx = actionRowIndex(item);
            return (
              <button
                key={`best:${item.actionKind}:${i}`}
                type="button"
                className={`palette-row palette-best-match-item${idx === activeIndex ? ' active' : ''}`}
                data-testid="palette-best-match-item"
                data-action-kind={item.actionKind}
                data-source-column={item.sourceColumn ?? ''}
                data-active={idx === activeIndex ? 'true' : 'false'}
                onMouseEnter={() => idx >= 0 && onHover(idx)}
                onClick={() => onLaunchAction(item)}
              >
                <span className="palette-row-name">{item.name}</span>
                {item.sourceColumn && (
                  <span className="palette-row-hint">{item.sourceColumn}</span>
                )}
                {i === 0 && (
                  <span className="palette-best-match-chip" data-testid="palette-best-match-chip">
                    <CornerDownLeft size={11} /> run
                  </span>
                )}
              </button>
            );
          })}
        </div>
      )}

      {filteredActions.length > 0 && (
        <div className="palette-section" data-testid="palette-section-actions">
          <div className="palette-section-label">Actions</div>
          {filteredActions.map((item, i) => {
            const idx = actionRowIndex(item);
            return (
              <button
                key={`act:${item.actionKind}:${i}`}
                type="button"
                className={`palette-row palette-action-item${idx === activeIndex ? ' active' : ''}`}
                data-testid="palette-action-item"
                data-action-kind={item.actionKind}
                data-active={idx === activeIndex ? 'true' : 'false'}
                onMouseEnter={() => idx >= 0 && onHover(idx)}
                onClick={() => onLaunchAction(item)}
              >
                <span className="palette-row-name">{item.name}</span>
                <span className="palette-row-affordance">⇥ configure</span>
              </button>
            );
          })}
        </div>
      )}

      {filteredGoto.length > 0 && (
        <div className="palette-section" data-testid="palette-section-goto">
          <div className="palette-section-label">Go to</div>
          {filteredGoto.map((item) => {
            const idx = gotoRowIndex(item);
            return (
              <button
                key={`goto:${item.sheetId}`}
                type="button"
                className={`palette-row palette-goto-item${idx === activeIndex ? ' active' : ''}`}
                data-testid="palette-goto-item"
                data-sheet-id={item.sheetId}
                data-active={idx === activeIndex ? 'true' : 'false'}
                onMouseEnter={() => idx >= 0 && onHover(idx)}
                onClick={() => onNavigateSheet(item)}
              >
                <span className="palette-row-name">{item.name}</span>
              </button>
            );
          })}
        </div>
      )}
    </>
  );
}

/** COMMANDS — first-party command entries, plugin launchers, and the
 *  hide/reveal visibility toggles. Query-filtered like the sibling sections
 *  and joined into the same flat arrow-key traversal via the
 *  row-index/active-index props threaded in from the parent. Renders
 *  nothing once filtering leaves it empty, so a narrow query doesn't leave
 *  a dangling "Commands" header with no rows under it. */
export function PaletteCommandsSection({
  commands,
  launcherCommands,
  hideTargets,
  revealTargets,
  activeIndex,
  commandRowIndex,
  launcherRowIndex,
  visibilityRowIndex,
  onHover,
  onLaunchCommand,
  onLaunchLauncher,
  onHideContribution,
  onRevealContribution,
}: {
  commands: WorkbenchCommandEntry[];
  launcherCommands: PaletteLauncherCommand[];
  hideTargets: WorkbenchVisibilityTarget[];
  revealTargets: WorkbenchVisibilityTarget[];
  activeIndex: number;
  commandRowIndex(entry: WorkbenchCommandEntry): number;
  launcherRowIndex(item: PaletteLauncherCommand): number;
  visibilityRowIndex(action: 'hide' | 'reveal', target: WorkbenchVisibilityTarget): number;
  onHover(index: number): void;
  onLaunchCommand(entry: WorkbenchCommandEntry): void;
  onLaunchLauncher(item: PaletteLauncherCommand): void;
  onHideContribution(target: WorkbenchVisibilityTarget): void;
  onRevealContribution(target: WorkbenchVisibilityTarget): void;
}) {
  if (
    commands.length === 0 &&
    launcherCommands.length === 0 &&
    hideTargets.length === 0 &&
    revealTargets.length === 0
  ) {
    return null;
  }

  return (
    <div className="palette-section" data-testid="palette-section-commands">
      <div className="palette-section-label">Commands</div>
      <ul className="workbench-command-palette-list">
        {commands.map((entry) => {
          const idx = commandRowIndex(entry);
          return (
            <li key={entry.descriptor.id}>
              <WorkbenchCommandButtonFrame
                descriptor={entry.descriptor}
                onClick={() => onLaunchCommand(entry)}
                disabled={entry.disabled}
                availabilityStatus={entry.availabilityStatus}
                availabilityReason={entry.availabilityReason}
                active={idx === activeIndex}
                onMouseEnter={() => idx >= 0 && onHover(idx)}
              />
            </li>
          );
        })}
        {launcherCommands.map((launcher) => {
          const idx = launcherRowIndex(launcher);
          return (
            <li key={`launcher:${launcher.contributionId}`}>
              <button
                type="button"
                className={`workbench-command-palette-item${idx === activeIndex ? ' active' : ''}`}
                data-testid={`workbench-launcher-command-${contributionSlug(launcher.contributionId)}`}
                data-command-id="frisket.core.command.reveal_launcher"
                data-target-contribution-id={launcher.contributionId}
                data-active={idx === activeIndex ? 'true' : 'false'}
                onMouseEnter={() => idx >= 0 && onHover(idx)}
                onClick={() => onLaunchLauncher(launcher)}
              >
                Open {launcher.label}
              </button>
            </li>
          );
        })}
        {hideTargets.map((target) => {
          const idx = visibilityRowIndex('hide', target);
          return (
            <li key={`hide:${target.contributionId}`}>
              <button
                type="button"
                className={`workbench-command-palette-item${idx === activeIndex ? ' active' : ''}`}
                data-testid={`workbench-visibility-command-hide-${contributionSlug(target.contributionId)}`}
                data-command-id="frisket.core.command.hide_contribution"
                data-target-contribution-id={target.contributionId}
                data-target-host={target.host}
                data-target-mode={target.mode}
                data-availability-status={target.status}
                data-availability-reason={target.reason}
                data-active={idx === activeIndex ? 'true' : 'false'}
                onMouseEnter={() => idx >= 0 && onHover(idx)}
                onClick={() => onHideContribution(target)}
              >
                {visibilityCommandLabel('hide', target)}
              </button>
            </li>
          );
        })}
        {revealTargets.map((target) => {
          const idx = visibilityRowIndex('reveal', target);
          return (
            <li key={`reveal:${target.contributionId}`}>
              <button
                type="button"
                className={`workbench-command-palette-item${idx === activeIndex ? ' active' : ''}`}
                data-testid={`workbench-visibility-command-reveal-${contributionSlug(target.contributionId)}`}
                data-command-id="frisket.core.command.reveal_contribution"
                data-target-contribution-id={target.contributionId}
                data-target-host={target.host}
                data-target-mode={target.mode}
                data-availability-status={target.status}
                data-availability-reason={target.reason}
                data-active={idx === activeIndex ? 'true' : 'false'}
                onMouseEnter={() => idx >= 0 && onHover(idx)}
                onClick={() => onRevealContribution(target)}
              >
                {visibilityCommandLabel('reveal', target)}
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
