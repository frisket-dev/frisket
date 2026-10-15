import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { listWalkthroughs } from '../api/walkthroughs';
import { useShellIdentity } from '../shellIdentity';
import { useTelemetryDisclosureReady } from '../telemetry/disclosureReady';
import { WalkthroughProvider } from '../walkthrough/WalkthroughProvider';
import { GuidanceReadyContext } from '../workbench/guidanceReady';
import { CostPreapprovalSetupModal } from './CostPreapprovalSetupModal';
import { FirstLaunchCloudSuggestion } from './FirstLaunchCloudSuggestion';

export function ApplicationGuidance({ projectId, children }: { projectId?: string; children: ReactNode }) {
  const { identityMode, me, resolved } = useShellIdentity();
  const telemetryReady = useTelemetryDisclosureReady();
  const [completedCostSetupFor, setCompletedCostSetupFor] = useState<string | null>(null);
  const [cloudWelcomeComplete, setCloudWelcomeComplete] = useState(false);
  const completeCloudWelcome = useCallback(() => setCloudWelcomeComplete(true), []);
  const [badges, setBadges] = useState<readonly { id: string; badges: readonly string[] }[]>([]);
  const needsCostSetup = identityMode && me?.cost_preapproval_usd === null
    && completedCostSetupFor !== me.email;
  const needsCloudWelcome = !identityMode && !cloudWelcomeComplete;

  useEffect(() => {
    let active = true;
    void listWalkthroughs().then((catalog) => {
      if (active) setBadges(catalog.walkthroughs);
    }).catch(() => {
      // Descriptive badges are optional; the existing guides remain usable.
    });
    return () => { active = false; };
  }, []);

  return (
    <GuidanceReadyContext.Provider value={resolved && telemetryReady && !needsCostSetup && !needsCloudWelcome}>
      <WalkthroughProvider projectId={projectId} walkthroughBadges={badges}>
        {children}
        {resolved && telemetryReady && needsCloudWelcome && (
          <FirstLaunchCloudSuggestion projectId={projectId} onComplete={completeCloudWelcome} />
        )}
        {telemetryReady && needsCostSetup && (
          <CostPreapprovalSetupModal onComplete={() => setCompletedCostSetupFor(me.email)} />
        )}
      </WalkthroughProvider>
    </GuidanceReadyContext.Provider>
  );
}
