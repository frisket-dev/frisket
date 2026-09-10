// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { useState, type ReactElement } from 'react';
import {
  cleanup,
  fireEvent,
  render as testingLibraryRender,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { WalkthroughProvider } from '../../src/walkthrough/WalkthroughProvider';
import { useWalkthrough } from '../../src/walkthrough/context';
import { resolveWalkthroughTarget } from '../../src/walkthrough/targets';
import { walkthroughToMarkdown } from '../../src/walkthrough/guide';
import {
  COMBINE_VALUES_WALKTHROUGH,
  LAWSUIT_DOCUMENT_WALKTHROUGH,
  LOCAL_MODEL_LAB_WALKTHROUGH,
  MULTILINGUAL_NAMES_WALKTHROUGH,
  REGEX_EXTRACT_WALKTHROUGH,
  WALKTHROUGHS,
} from '../../src/walkthrough/walkthroughs';
import {
  defineEditionModule,
  EditionModuleProvider,
  type EditionModule,
} from '../../src/editions/module';
import { LOCAL_EDITION_MODULE } from '../../src/editions/openModules';

const OPERATOR_EDITION_MODULE = defineEditionModule({
  descriptor: {
    id: 'operator',
    capabilities: {
      configurableNotificationDestinations: false,
      configurableNotificationEmail: false,
      identity: true,
      team: true,
    },
  },
});

function renderWithEdition(
  ui: ReactElement,
  edition: Readonly<EditionModule> = LOCAL_EDITION_MODULE,
) {
  return testingLibraryRender(
    <EditionModuleProvider edition={edition}>{ui}</EditionModuleProvider>,
  );
}

const render = renderWithEdition;

function Launcher() {
  const walkthrough = useWalkthrough();
  return (
    <>
      <button
        type="button"
        data-testid="test-guide"
        data-seen={walkthrough.guideSeen ? 'true' : 'false'}
        onClick={walkthrough.openWalkthroughChooser}
      >
        Start
      </button>
      {walkthrough.canResume && (
        <button type="button" onClick={walkthrough.resumeWalkthrough}>Resume</button>
      )}
    </>
  );
}

function HiddenGuideLauncher() {
  const walkthrough = useWalkthrough();
  return (
    <>
      <button
        type="button"
        onClick={() => walkthrough.startWalkthrough('local-model-lab')}
      >
        Start hidden
      </button>
      <span data-testid="hidden-can-resume">
        {walkthrough.canResume ? 'true' : 'false'}
      </span>
    </>
  );
}

function StopControl() {
  const walkthrough = useWalkthrough();
  return <button type="button" onClick={walkthrough.stopWalkthrough}>Stop test run</button>;
}

const recoveryKey = (projectId: string) => (
  `frisket.walkthrough.active-run.v1:${projectId}`
);

function storeRecovery(
  projectId: string,
  walkthroughId = REGEX_EXTRACT_WALKTHROUGH.id,
  stepIndex = 1,
  updatedAt = Date.now(),
) {
  window.localStorage.setItem(recoveryKey(projectId), JSON.stringify({
    walkthroughId,
    stepIndex,
    projectId,
    updatedAt,
  }));
}

function ReplacingSheetTarget() {
  const [sheetVisible, setSheetVisible] = useState(true);
  return sheetVisible ? (
    <button
      type="button"
      data-testid="workbench-mainView-tab-41"
      onClick={() => setSheetVisible(false)}
    >
      <span className="workbench-mainView-tab-name">Dispatches</span>
    </button>
  ) : (
    <button type="button" data-testid="view-switch-grid">Grid</button>
  );
}

beforeEach(() => {
  HTMLElement.prototype.scrollIntoView = vi.fn();
  window.localStorage.clear();
});

afterEach(() => {
  cleanup();
  document.body.replaceChildren();
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
});

describe('walkthrough targets', () => {
  it('uses the action surface that is currently rendered', () => {
    const compact = document.createElement('button');
    compact.dataset.testid = 'menubar-menu-extract';
    document.body.append(compact);

    expect(resolveWalkthroughTarget({ kind: 'action-tab', id: 'extract' })).toBe(compact);

    const ribbon = document.createElement('button');
    ribbon.dataset.testid = 'ribbon-tab-extract';
    document.body.prepend(ribbon);

    expect(resolveWalkthroughTarget({ kind: 'action-tab', id: 'extract' })).toBe(ribbon);
  });

  it('ignores a matching control inside a hidden ancestor', () => {
    const hiddenParent = document.createElement('div');
    hiddenParent.style.display = 'none';
    const hidden = document.createElement('button');
    hidden.dataset.testid = 'ribbon-tab-extract';
    hiddenParent.append(hidden);
    document.body.append(hiddenParent);

    const visible = document.createElement('button');
    visible.dataset.testid = 'ribbon-tab-extract';
    document.body.append(visible);

    expect(resolveWalkthroughTarget({ kind: 'action-tab', id: 'extract' })).toBe(visible);
  });

  it('notices a target whose test id changes in place after loading', async () => {
    render(
      <WalkthroughProvider>
        <Launcher />
        <button type="button" data-testid="sheet-loading">
          <span className="workbench-mainView-tab-name">Dispatches</span>
        </button>
      </WalkthroughProvider>,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Start' }));
    fireEvent.click(screen.getByTestId('walkthrough-choice-regex-extract'));
    expect(screen.getByRole('status')).toHaveTextContent('This control is not visible yet');
    expect(screen.getByText('Click the highlighted control')).toBeInTheDocument();

    screen.getByTestId('sheet-loading').dataset.testid = 'workbench-mainView-tab-41';
    await waitFor(() => {
      expect(screen.getByTestId('walkthrough-target-box')).toHaveAttribute(
        'data-target-testid',
        'workbench-mainView-tab-41',
      );
    });
  });

  it('finds a sheet tab by its visible name even when the active tab has a row count', () => {
    const tab = document.createElement('button');
    tab.dataset.testid = 'workbench-mainView-tab-41';
    tab.innerHTML = '<span class="workbench-mainView-tab-name">Dispatches</span><span>24</span>';
    document.body.append(tab);

    expect(resolveWalkthroughTarget({ kind: 'sheet', id: 'Dispatches' })).toBe(tab);
  });

  it('resolves a semantic grid-column target', () => {
    const column = document.createElement('span');
    column.dataset.testid = 'grid-column-story';
    column.dataset.walkthroughGridColumn = 'story';
    document.body.append(column);

    expect(resolveWalkthroughTarget({ kind: 'grid-column', id: 'story' })).toBe(column);
  });

  it('resolves a Discover tab from either the open panel or collapsed rail', () => {
    const rail = document.createElement('button');
    rail.dataset.testid = 'discover-rail-icon-Facets';
    document.body.append(rail);
    expect(resolveWalkthroughTarget({ kind: 'discover-tab', id: 'Facets' })).toBe(rail);

    const tab = document.createElement('button');
    tab.dataset.testid = 'discover-tab-Facets';
    document.body.prepend(tab);
    expect(resolveWalkthroughTarget({ kind: 'discover-tab', id: 'Facets' })).toBe(tab);
  });

  it('resolves the first visible stable tour anchor', () => {
    const hidden = document.createElement('div');
    hidden.dataset.tour = 'local-endpoint-row';
    hidden.hidden = true;
    const visible = document.createElement('div');
    visible.dataset.tour = 'local-endpoint-row';
    document.body.append(hidden, visible);

    expect(resolveWalkthroughTarget({ kind: 'tour', id: 'local-endpoint-row' })).toBe(visible);
  });

  it('resolves a named endpoint instead of the first endpoint', () => {
    const existing = document.createElement('div');
    existing.dataset.tour = 'local-endpoint-row';
    existing.dataset.endpointLabel = 'Existing newsroom server';
    const expected = document.createElement('div');
    expected.dataset.tour = 'local-endpoint-row';
    expected.dataset.endpointLabel = 'Local model lab server';
    const existingGuidance = document.createElement('div');
    existingGuidance.dataset.tour = 'local-endpoint-guidance';
    existingGuidance.dataset.endpointLabel = 'Existing newsroom server';
    const expectedGuidance = document.createElement('div');
    expectedGuidance.dataset.tour = 'local-endpoint-guidance';
    expectedGuidance.dataset.endpointLabel = 'Local model lab server';
    document.body.append(existing, expected, existingGuidance, expectedGuidance);

    expect(resolveWalkthroughTarget({
      kind: 'local-endpoint',
      label: 'Local model lab server',
      part: 'row',
    })).toBe(expected);
    expect(resolveWalkthroughTarget({
      kind: 'local-endpoint',
      label: 'Local model lab server',
      part: 'guidance',
    })).toBe(expectedGuidance);
  });

  it('waits for a successfully completed run before revealing its result target', () => {
    const grid = document.createElement('div');
    grid.dataset.testid = 'grid';
    document.body.append(grid);
    const run = document.createElement('button');
    run.dataset.testid = 'run-watcher-toggle';
    run.dataset.runStatus = 'running';
    run.dataset.runCompletedRows = '2';
    run.dataset.runTotalRows = '6';
    run.dataset.runFailedRows = '0';
    document.body.append(run);
    const target = {
      kind: 'completed-run' as const,
      target: { kind: 'element' as const, id: 'grid' },
    };

    expect(resolveWalkthroughTarget(target)).toBeNull();
    run.dataset.runStatus = 'complete';
    expect(resolveWalkthroughTarget(target)).toBe(grid);
    run.dataset.runFailedRows = '1';
    expect(resolveWalkthroughTarget(target)).toBeNull();
  });
});

describe('Regex walkthrough', () => {
  it('keeps unresolved read-only steps instructional without click guidance', async () => {
    render(
      <WalkthroughProvider>
        <Launcher />
        <button type="button" data-testid="workbench-mainView-tab-42">
          <span className="workbench-mainView-tab-name">Small model lab</span>
        </button>
      </WalkthroughProvider>,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Start' }));
    fireEvent.click(screen.getByTestId('walkthrough-choice-local-model-lab'));
    fireEvent.click(screen.getByTestId('workbench-mainView-tab-42'));

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Start with the easy notices' })).toBeVisible();
    });
    expect(screen.getByText(/Read easy_notice and expected_notice_type/)).toBeVisible();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(screen.queryByText('Click the highlighted control')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Next' })).toBeEnabled();
  });

  it('does not offer, start, or make a hidden guide resumable', () => {
    renderWithEdition(
      <WalkthroughProvider>
        <Launcher />
        <HiddenGuideLauncher />
      </WalkthroughProvider>,
      OPERATOR_EDITION_MODULE,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Start' }));
    expect(screen.queryByTestId('walkthrough-choice-local-model-lab')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Close walkthrough chooser' }));
    fireEvent.click(screen.getByRole('button', { name: 'Start hidden' }));
    expect(screen.queryByTestId('walkthrough-card')).not.toBeInTheDocument();
    expect(screen.getByTestId('hidden-can-resume')).toHaveTextContent('false');
  });

  it('keeps tour and step identities unique and renders every tour as a guide', () => {
    expect(new Set(WALKTHROUGHS.map((tour) => tour.id)).size).toBe(WALKTHROUGHS.length);
    for (const tour of WALKTHROUGHS) {
      expect(new Set(tour.steps.map((step) => step.id)).size).toBe(tour.steps.length);
      expect(walkthroughToMarkdown(tour)).toContain(`# ${tour.title}`);
    }
  });

  it('renders its authored steps as a written guide', () => {
    const markdown = walkthroughToMarkdown(REGEX_EXTRACT_WALKTHROUGH);
    expect(markdown).toContain('# Follow contracts across sheets');
    expect(markdown).toContain('## 1. Open Dispatches');
    expect(markdown).toContain('## 3. Read the source stories');
    expect(markdown).toContain('## 4. Open Extract');
    expect(markdown).toContain('## 20. Inspect the contract trail');
    expect(markdown).toContain('**Then:** Stories with a contract reference');
  });

  it('teaches the model-free transliteration and reviewed-substitution boundary', () => {
    const markdown = walkthroughToMarkdown(MULTILINGUAL_NAMES_WALKTHROUGH);
    expect(markdown).toContain('# Standardize a multilingual name list');
    expect(markdown).toContain('Transliteration changes scripts');
    expect(markdown).toContain('Transliteration alone cannot safely make it for you');
    expect(markdown).toContain('`Alexander Petrov`');
    expect(markdown).toContain('latin_text_clean');
  });

  it('renders the Combine values workflow and its before-and-after counting payoff', () => {
    const markdown = walkthroughToMarkdown(COMBINE_VALUES_WALKTHROUGH);
    expect(markdown).toContain('# Clean up a messy agency column');
    expect(markdown).toContain('12 exact values');
    expect(markdown).toContain('Dotted leading or trailing regions');
    expect(markdown).toContain('Public Works Department 6');
    expect(markdown).toContain('agency_clean');
  });

  it('renders local-model setup, easy-task, and capability-limit steps from one definition', () => {
    const markdown = walkthroughToMarkdown(LOCAL_MODEL_LAB_WALKTHROUGH);
    expect(markdown).toContain('# Learn what a small local model can — and cannot — do');
    expect(markdown).toContain('`http://localhost:11434`');
    expect(markdown).toContain('`ollama pull qwen3:0.6b`');
    expect(markdown).toContain('small_model_label');
    expect(markdown).toContain('challenge_answer');
    expect(markdown).toContain('has no provider charge in Frisket');
    expect(markdown).not.toContain('Acknowledge the local runtime');
    expect(markdown).not.toContain('Because local runtime cost is unknown');
    expect(markdown).toContain('larger local model, a cloud model, or a deterministic rule');
  });

  it('explains the lawsuit case-number regex with code and an external learning link', () => {
    render(
      <WalkthroughProvider>
        <Launcher />
      </WalkthroughProvider>,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Start' }));
    fireEvent.click(screen.getByTestId('walkthrough-choice-lawsuit-documents'));
    const patternStep = LAWSUIT_DOCUMENT_WALKTHROUGH.steps.findIndex(
      (step) => step.id === 'enter-case-pattern',
    );
    for (let index = 0; index < patternStep; index += 1) {
      fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    }

    expect(screen.getByRole('heading', { name: 'Enter the case-number pattern' })).toBeVisible();
    const code = screen.getByText('RV-CV-\\d{4}-\\d{5}');
    expect(code.tagName).toBe('CODE');
    expect(code.closest('p')).toHaveTextContent(
      'This is a regular expression. It means RV-CV-(four digits)-(five digits).',
    );
    const link = within(code.closest('p')!).getByRole('link', {
      name: 'Learn more about regular expressions',
    });
    expect(link).toHaveAttribute('href', 'https://docs.python.org/3/howto/regex.html');
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  });

  it('advances through the real tab and submenu clicks', async () => {
    render(
      <WalkthroughProvider>
        <Launcher />
        <button type="button" data-testid="workbench-mainView-tab-41">
          <span className="workbench-mainView-tab-name">Dispatches</span>
        </button>
        <button type="button" data-testid="view-switch-grid">Grid</button>
        <span data-testid="grid-column-story" data-walkthrough-grid-column="story" />
        <button type="button" data-testid="ribbon-tab-extract">Extract</button>
        <button type="button" data-testid="ribbon-action-regex">Regex extract</button>
        <button type="button" data-testid="text-source-column-select">Story</button>
      </WalkthroughProvider>,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Start' }));
    expect(screen.getByTestId('walkthrough-choice-lawsuit-documents')).toBeInTheDocument();
    expect(screen.getByTestId('walkthrough-choice-council-audio')).toBeInTheDocument();
    expect(screen.getByTestId('walkthrough-choice-civic-ai-triage')).toBeInTheDocument();
    expect(screen.getByTestId('walkthrough-choice-rss-import')).toBeInTheDocument();
    expect(screen.getByTestId('walkthrough-choice-multilingual-names')).toBeInTheDocument();
    expect(screen.getByTestId('walkthrough-choice-combine-values')).toBeInTheDocument();
    expect(screen.getByTestId('walkthrough-choice-local-model-lab')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('walkthrough-choice-regex-extract'));
    expect(screen.getByRole('heading', { name: 'Open Dispatches' })).toBeInTheDocument();

    fireEvent.click(screen.getByTestId('workbench-mainView-tab-41'));
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Start in Grid' })).toBeInTheDocument();
      expect(screen.getByTestId('walkthrough-target-box')).toHaveAttribute(
        'data-target-testid',
        'view-switch-grid',
      );
    });

    fireEvent.click(screen.getByTestId('view-switch-grid'));
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Read the source stories' })).toBeInTheDocument();
      expect(screen.getByTestId('walkthrough-target-box')).toHaveAttribute(
        'data-target-testid',
        'grid-column-story',
      );
    });
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Open Extract' })).toBeInTheDocument();
      expect(screen.getByTestId('walkthrough-target-box')).toHaveAttribute(
        'data-target-testid',
        'ribbon-tab-extract',
      );
    });

    fireEvent.click(screen.getByTestId('ribbon-tab-extract'));
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Choose Regex extract' })).toBeInTheDocument();
      expect(screen.getByTestId('walkthrough-target-box')).toHaveAttribute(
        'data-target-testid',
        'ribbon-action-regex',
      );
    });

    fireEvent.click(screen.getByTestId('ribbon-action-regex'));
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Choose the source text' })).toBeInTheDocument();
      expect(screen.getByTestId('walkthrough-target-box')).toHaveAttribute(
        'data-target-testid',
        'text-source-column-select',
      );
    });
  });

  it('advances when a click immediately replaces the highlighted control', async () => {
    render(
      <WalkthroughProvider>
        <Launcher />
        <ReplacingSheetTarget />
      </WalkthroughProvider>,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Start' }));
    fireEvent.click(screen.getByTestId('walkthrough-choice-regex-extract'));
    fireEvent.click(screen.getByTestId('workbench-mainView-tab-41'));

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Start in Grid' })).toBeInTheDocument();
      expect(screen.getByTestId('walkthrough-target-box')).toHaveAttribute(
        'data-target-testid',
        'view-switch-grid',
      );
    });
  });

  it('offers Next on click-driven steps and resumes at the paused step', () => {
    render(
      <WalkthroughProvider>
        <Launcher />
        <button type="button" data-testid="workbench-mainView-tab-41">
          <span className="workbench-mainView-tab-name">Dispatches</span>
        </button>
        <button type="button" data-testid="view-switch-grid">Grid</button>
        <span data-testid="grid-column-story" data-walkthrough-grid-column="story" />
        <button type="button" data-testid="ribbon-tab-extract">Extract</button>
        <button type="button" data-testid="ribbon-action-regex">Regex extract</button>
      </WalkthroughProvider>,
    );

    expect(screen.getByTestId('test-guide')).toHaveAttribute('data-seen', 'false');
    fireEvent.click(screen.getByRole('button', { name: 'Start' }));
    expect(screen.getByTestId('test-guide')).toHaveAttribute('data-seen', 'true');
    expect(window.localStorage.getItem('frisket.walkthrough.guide-seen.v1')).toBe('true');
    fireEvent.click(screen.getByTestId('walkthrough-choice-regex-extract'));

    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(screen.getByRole('heading', { name: 'Start in Grid' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(screen.getByRole('heading', { name: 'Read the source stories' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(screen.getByRole('heading', { name: 'Open Extract' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(screen.getByRole('heading', { name: 'Choose Regex extract' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Pause walkthrough' }));
    expect(screen.queryByTestId('walkthrough-card')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Resume' }));
    expect(screen.getByRole('heading', { name: 'Choose Regex extract' })).toBeInTheDocument();
  });

  it('restores a matching project paused and re-resolves its target on resume', async () => {
    const firstMount = render(
      <WalkthroughProvider projectId="project-a">
        <Launcher />
      </WalkthroughProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Start' }));
    fireEvent.click(screen.getByTestId('walkthrough-choice-regex-extract'));
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    await waitFor(() => expect(window.localStorage.getItem(recoveryKey('project-a'))).not.toBeNull());
    firstMount.unmount();

    render(
      <WalkthroughProvider projectId="project-a">
        <Launcher />
      </WalkthroughProvider>,
    );
    expect(screen.queryByTestId('walkthrough-card')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }));
    expect(screen.getByRole('heading', { name: 'Start in Grid' })).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('This control is not visible yet');

    const target = document.createElement('button');
    target.dataset.testid = 'view-switch-grid';
    document.body.append(target);
    await waitFor(() => {
      expect(screen.getByTestId('walkthrough-target-box')).toHaveAttribute(
        'data-target-testid',
        'view-switch-grid',
      );
    });
  });

  it('does not restore another project or an expired, malformed, out-of-range, or hidden run', () => {
    storeRecovery('project-a');
    const wrongProject = render(
      <WalkthroughProvider projectId="project-b">
        <Launcher />
      </WalkthroughProvider>,
    );
    expect(screen.queryByRole('button', { name: 'Resume' })).not.toBeInTheDocument();
    wrongProject.unmount();

    storeRecovery(
      'project-b',
      REGEX_EXTRACT_WALKTHROUGH.id,
      1,
      Date.now() - 7 * 24 * 60 * 60 * 1000 - 1,
    );
    const expired = render(
      <WalkthroughProvider projectId="project-b">
        <Launcher />
      </WalkthroughProvider>,
    );
    expect(screen.queryByRole('button', { name: 'Resume' })).not.toBeInTheDocument();
    expect(window.localStorage.getItem(recoveryKey('project-b'))).toBeNull();
    expired.unmount();

    window.localStorage.setItem(recoveryKey('project-b'), '{not json');
    const malformed = render(
      <WalkthroughProvider projectId="project-b">
        <Launcher />
      </WalkthroughProvider>,
    );
    expect(screen.queryByRole('button', { name: 'Resume' })).not.toBeInTheDocument();
    expect(window.localStorage.getItem(recoveryKey('project-b'))).toBeNull();
    malformed.unmount();

    storeRecovery('project-b', REGEX_EXTRACT_WALKTHROUGH.id, 99_999);
    const outOfRange = render(
      <WalkthroughProvider projectId="project-b">
        <Launcher />
      </WalkthroughProvider>,
    );
    expect(screen.queryByRole('button', { name: 'Resume' })).not.toBeInTheDocument();
    expect(window.localStorage.getItem(recoveryKey('project-b'))).toBeNull();
    outOfRange.unmount();

    storeRecovery('project-b', LOCAL_MODEL_LAB_WALKTHROUGH.id);
    renderWithEdition(
      <WalkthroughProvider projectId="project-b">
        <Launcher />
      </WalkthroughProvider>,
      OPERATOR_EDITION_MODULE,
    );
    expect(screen.queryByRole('button', { name: 'Resume' })).not.toBeInTheDocument();
    expect(window.localStorage.getItem(recoveryKey('project-b'))).toBeNull();
  });

  it('clears project recovery when stopped or finished', async () => {
    const active = render(
      <WalkthroughProvider projectId="project-a">
        <Launcher />
        <StopControl />
      </WalkthroughProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Start' }));
    fireEvent.click(screen.getByTestId('walkthrough-choice-regex-extract'));
    await waitFor(() => expect(window.localStorage.getItem(recoveryKey('project-a'))).not.toBeNull());
    fireEvent.click(screen.getByRole('button', { name: 'Stop test run' }));
    expect(window.localStorage.getItem(recoveryKey('project-a'))).toBeNull();
    active.unmount();

    storeRecovery(
      'project-a',
      REGEX_EXTRACT_WALKTHROUGH.id,
      REGEX_EXTRACT_WALKTHROUGH.steps.length - 1,
    );
    render(
      <WalkthroughProvider projectId="project-a">
        <Launcher />
      </WalkthroughProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }));
    fireEvent.click(screen.getByRole('button', { name: 'Finish' }));
    expect(window.localStorage.getItem(recoveryKey('project-a'))).toBeNull();
  });

  it('moves the guide card when its header is dragged', () => {
    render(
      <WalkthroughProvider>
        <Launcher />
        <button type="button" data-testid="ribbon-tab-extract">Extract</button>
      </WalkthroughProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Start' }));
    fireEvent.click(screen.getByTestId('walkthrough-choice-regex-extract'));

    const card = screen.getByTestId('walkthrough-card');
    const startLeft = Number.parseFloat(card.style.left);
    const startTop = Number.parseFloat(card.style.top);
    const handle = screen.getByTestId('walkthrough-drag-handle');
    fireEvent.pointerDown(handle, {
      button: 0, pointerId: 9, clientX: 100, clientY: 100,
    });
    fireEvent.pointerMove(handle, {
      pointerId: 9, clientX: 180, clientY: 150,
    });
    fireEvent.pointerUp(handle, { pointerId: 9 });

    expect(Number.parseFloat(card.style.left)).toBeGreaterThan(startLeft);
    expect(Number.parseFloat(card.style.top)).toBeGreaterThan(startTop);
  });

  it('starts a newly selected guide at its first step', () => {
    render(
      <WalkthroughProvider>
        <Launcher />
      </WalkthroughProvider>,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Start' }));
    fireEvent.click(screen.getByTestId('walkthrough-choice-regex-extract'));
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(screen.getByText('3 of 20')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Start' }));
    fireEvent.click(screen.getByTestId('walkthrough-choice-council-audio'));
    expect(screen.getByRole('heading', { name: 'Open the meeting audio' })).toBeInTheDocument();
    expect(screen.getByText('1 of 21')).toBeInTheDocument();
  });
});
