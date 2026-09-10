// A persistent top bar — never a toast — shown whenever the active router cache
// mode means live AI calls cannot happen. That is ONLY strict replay
// (live_calls_possible=false): plain replay is cache-first but falls through to
// a live call on a miss, so it is NOT a no-live-calls posture and shows no bar.
// It can collapse to a slim strip, but there is no "dismiss forever" control:
// while the condition holds, some form of the bar always renders, every time
// the app loads.
//
// The bar renders in normal document flow (a flex child of .app-shell in
// App.tsx) so it pushes page content down rather than overlaying it; the
// AI-call-mode explainer lives in Settings → Preferences, not here.
//
// It distinguishes two root causes: with NO providers configured the fix is to
// add one, so it links to the AI-providers page; with providers configured the
// blocker is the call MODE, so it links to the mode explainer in Preferences.
import { useEffect, useState } from 'react';
import { AlertTriangle, ChevronDown, ChevronUp } from 'lucide-react';
import {
  getRuntimeConfig,
  listOrgKeys,
  listProviders,
  onRuntimeConfigChanged,
  type RuntimeConfig,
} from '../api/open';
import { createProjectProviderKeysApi } from '../api/projectProviderKeys';
import { useEditionModule } from '../editions/module';
import { navigate, useRoute } from '../routes';

/** Configured provider labels for an ARBITRARY project id (the banner probes
 * projects other than the active one). Hoisted to module scope so the network
 * call lives outside the effect (react-doctor no-fetch-in-effect); a missing or
 * failed response yields an empty list rather than throwing. */
const projectProviderKeysApi = createProjectProviderKeysApi();

async function fetchProjectProviderKeyLabels(projectId: string): Promise<string[]> {
  try {
    const payload = await projectProviderKeysApi.getProjectProviderKeys(projectId);
    const labels: string[] = [];
    for (const provider of payload.providers) {
      if (provider.configured) labels.push(provider.label || provider.id || 'provider');
    }
    return labels;
  } catch {
    return [];
  }
}

export function ReplayModeBanner() {
  const { descriptor } = useEditionModule();
  const [config, setConfig] = useState<RuntimeConfig | null>(null);
  const [collapsed, setCollapsed] = useState(false);
  const [providerSnapshot, setProviderSnapshot] = useState<{ key: string; providers: string[] } | null>(null);
  const route = useRoute();

  useEffect(() => {
    let alive = true;
    const unsubscribe = onRuntimeConfigChanged((updated) => {
      if (alive) setConfig(updated);
    });
    getRuntimeConfig()
      .then((loaded) => {
        if (alive) setConfig(loaded);
      })
      .catch(() => undefined); // offline/mock: no posture to report, banner stays hidden
    return () => {
      alive = false;
      unsubscribe();
    };
  }, []);

  const visible = Boolean(config && !config.live_calls_possible);

  const projectId =
    route.kind === 'project'
      ? route.projectId
      : route.kind === 'settings' && route.scope === 'project'
        ? route.projectId
      : undefined;
  const providerSnapshotKey = projectId ?? '__global__';

  useEffect(() => {
    if (!visible) {
      return undefined;
    }
    let alive = true;
    const labels = new Set<string>();
    const addLocalProviders = async () => {
      const catalog = await listProviders();
      for (const provider of catalog.providers) {
        if (provider.kind === 'platform_api' && provider.configured) labels.add(provider.label);
      }
    };
    const addOrgProviders = async () => {
      const keys = await listOrgKeys();
      for (const key of keys) labels.add(key.provider);
    };
    const addProjectProviders = async () => {
      if (!projectId) return;
      for (const label of await fetchProjectProviderKeyLabels(projectId)) labels.add(label);
    };
    Promise.allSettled([addLocalProviders(), addOrgProviders(), addProjectProviders()])
      .then(() => {
        if (alive) {
          setProviderSnapshot({ key: providerSnapshotKey, providers: Array.from(labels).sort() });
        }
      });
    return () => {
      alive = false;
    };
  }, [projectId, providerSnapshotKey, visible]);

  if (!visible) return null;

  const openProviderSettings = () => {
    // Local tier: the workspace-level page owns provider keys + the Ollama
    // URL — the old organization target
    // is hosted-only and renders disabled locally. Hosted identities keep
    // their project-override / org-key destinations.
    navigate(
      !descriptor.capabilities.identity
        ? { kind: 'settings', scope: 'personal', section: 'ai-providers' }
        : projectId
          ? { kind: 'settings', projectId, scope: 'project', section: 'ai-providers' }
          : { kind: 'settings', scope: 'organization', section: 'ai-providers' },
    );
  };

  const openCallModeSettings = () => {
    // The AI-call-mode explainer lives on the personal Preferences page
    // (Settings → Preferences), the same target on local and hosted.
    navigate({ kind: 'settings', scope: 'personal', section: 'preferences' });
  };

  const configuredProviders = providerSnapshot?.key === providerSnapshotKey ? providerSnapshot.providers : [];
  const hasProviders = configuredProviders.length > 0;
  // The bar only renders when live calls are impossible, which today is
  // strict replay alone; describe whatever mode the server actually reports.
  const modeBlurb = config?.cache_mode === 'replay_strict'
    ? 'strict replay mode reuses cached responses only — a request with no cached response fails instead of calling a provider'
    : 'the current AI call mode makes no live AI calls';

  return (
    // <output> carries an implicit status role (react-doctor
    // prefer-tag-over-role) — no explicit role="status" needed.
    <output
      className={`replay-mode-banner${collapsed ? ' collapsed' : ''}`}
      data-testid="replay-mode-banner"
    >
      <AlertTriangle size={13} className="replay-mode-banner-icon" />
      {!collapsed && (
        <span className="replay-mode-banner-text" data-testid="replay-mode-banner-text">
          {hasProviders ? (
            <>
              {configuredProviders.join(', ')} configured, but {modeBlurb}.
              <button
                type="button"
                className="replay-mode-banner-setup-link"
                data-testid="replay-mode-banner-mode-link"
                onClick={openCallModeSettings}
              >
                AI call mode
              </button>
            </>
          ) : (
            <>
              No AI providers are configured, so no live AI calls can be made.
              <button
                type="button"
                className="replay-mode-banner-setup-link"
                data-testid="replay-mode-banner-setup-link"
                onClick={openProviderSettings}
              >
                Configure AI providers
              </button>
            </>
          )}
        </span>
      )}
      <button
        type="button"
        className="replay-mode-banner-toggle"
        data-testid="replay-mode-banner-toggle"
        title={collapsed ? 'Expand' : 'Collapse'}
        aria-label={collapsed ? 'Expand replay mode banner' : 'Collapse replay mode banner'}
        onClick={() => setCollapsed((current) => !current)}
      >
        {collapsed ? <ChevronDown size={12} /> : <ChevronUp size={12} />}
      </button>
    </output>
  );
}
