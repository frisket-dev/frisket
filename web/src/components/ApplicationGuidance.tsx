import './ApplicationGuidance.css';
import { useCallback, useState, type ReactNode } from 'react';
import { listWalkthroughs } from '../api/walkthroughs';
import { useLoader } from '../hooks/useLoader';
import { useShellIdentity } from '../shellIdentity';
import { useTelemetryDisclosureReady } from '../telemetry/disclosureReady';
import { WalkthroughProvider } from '../walkthrough/WalkthroughProvider';
import { GuidanceReadyContext } from '../workbench/guidanceReady';
import { AllowanceSetupModal } from './AllowanceSetupModal';
import { FirstLaunchCloudSuggestion } from './FirstLaunchCloudSuggestion';

const NO_BADGES: readonly { id: string; badges: readonly string[] }[] = [];

export function ApplicationGuidance({ projectId, children }: { projectId?: string; children: ReactNode }) {
  const { identityMode, me, resolved } = useShellIdentity();
  const telemetryReady = useTelemetryDisclosureReady();
  const [completedCostSetupFor, setCompletedCostSetupFor] = useState<string | null>(null);
  const [cloudWelcomeComplete, setCloudWelcomeComplete] = useState(false);
  const completeCloudWelcome = useCallback(() => setCloudWelcomeComplete(true), []);
  // Descriptive badges are optional; a failed catalog leaves the guides usable without them.
  const badges = useLoader(() => listWalkthroughs(), []).data?.walkthroughs ?? NO_BADGES;
  const needsCostSetup = identityMode && me?.allowance_usd === null
    && completedCostSetupFor !== me.email;
  const needsCloudWelcome = !identityMode && !cloudWelcomeComplete;

  return (
    <GuidanceReadyContext.Provider value={resolved && telemetryReady && !needsCostSetup && !needsCloudWelcome}>
      <WalkthroughProvider projectId={projectId} walkthroughBadges={badges}>
        {children}
        {resolved && telemetryReady && needsCloudWelcome && (
          <FirstLaunchCloudSuggestion projectId={projectId} onComplete={completeCloudWelcome} />
        )}
        {telemetryReady && needsCostSetup && (
          <AllowanceSetupModal onComplete={() => setCompletedCostSetupFor(me.email)} />
        )}
      </WalkthroughProvider>
    </GuidanceReadyContext.Provider>
  );
}
