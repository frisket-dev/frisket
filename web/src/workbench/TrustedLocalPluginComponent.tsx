import * as React from 'react';
import { Component } from 'react';
import { useEffect, useMemo, useState } from 'react';
import { loadTrustedLocalPluginExport } from './trustedLocalModule';

type TrustedLocalPluginExport = (props: {
  React: typeof React;
  ctx?: unknown;
  contributionId: string;
  pluginId: string;
}) => React.ReactNode;

interface TrustedLocalPluginComponentProps {
  moduleUrl: string;
  componentKey: string;
  contributionId: string;
  pluginId: string;
  ctx?: unknown;
}

type TrustedLocalPluginLoadState = {
  moduleUrl: string;
  component: TrustedLocalPluginExport | null;
  error: string | null;
} | null;

export class TrustedLocalContributionErrorBoundary extends Component<
  {
    contributionId: string;
    pluginId: string;
    moduleUrl: string;
    children: React.ReactNode;
  },
  { error: string | null }
> {
  state = { error: null };

  static getDerivedStateFromError(error: unknown) {
    return {
      error: error instanceof Error ? error.message : 'Trusted-local contribution failed',
    };
  }

  render() {
    if (this.state.error) {
      return (
        <div
          className="trusted-local-plugin-component trusted-local-plugin-component-error"
          data-testid="trusted-local-plugin-component-render-error"
          data-plugin-id={this.props.pluginId}
          data-contribution-id={this.props.contributionId}
          data-module-url={this.props.moduleUrl}
          data-error-boundary="trustedLocalContribution"
        >
          Trusted-local plugin UI failed to render.
        </div>
      );
    }
    return this.props.children;
  }
}

function testIdForContribution(contributionId: string): string {
  return `trusted-local-plugin-component-${contributionId
    .replace(/[^a-z0-9]+/gi, '-')
    .toLowerCase()}`;
}

export function TrustedLocalPluginComponent({
  moduleUrl,
  componentKey,
  contributionId,
  pluginId,
  ctx,
}: TrustedLocalPluginComponentProps) {
  const [loadState, setLoadState] = useState<TrustedLocalPluginLoadState>(null);
  const testId = useMemo(() => testIdForContribution(contributionId), [contributionId]);

  useEffect(() => {
    let active = true;

    loadTrustedLocalPluginExport(moduleUrl, componentKey)
      .then((candidate) => {
        if (!active) return;
        setLoadState({
          moduleUrl,
          component: candidate as TrustedLocalPluginExport,
          error: null,
        });
      })
      .catch((err: unknown) => {
        if (!active) return;
        setLoadState({
          moduleUrl,
          component: null,
          error: err instanceof Error ? err.message : 'Unable to load trusted-local component',
        });
      });

    return () => {
      active = false;
    };
  }, [moduleUrl, componentKey]);

  const Component = loadState?.moduleUrl === moduleUrl ? loadState.component : null;
  const error = loadState?.moduleUrl === moduleUrl ? loadState.error : null;

  if (error) {
    return (
      <div
        className="trusted-local-plugin-component trusted-local-plugin-component-error"
        data-testid="trusted-local-plugin-component-error"
        data-plugin-id={pluginId}
        data-contribution-id={contributionId}
        data-module-url={moduleUrl}
      >
        Trusted-local plugin UI failed to load.
      </div>
    );
  }

  if (!Component) {
    // Allowlisted, not migrated to PanelLoading — this is the loading half
    // of a base/error/loading class trio (trusted-local-plugin-component
    // fills the contribution host via flex; -error is its sibling) carrying
    // plugin-identifying data-* attributes plugin-ui-gauntlet.spec.ts
    // depends on; migrating only the loading half would break the trio's
    // symmetry.
    return (
      <div
        className="trusted-local-plugin-component trusted-local-plugin-component-loading"
        data-testid="trusted-local-plugin-component-loading"
        data-plugin-id={pluginId}
        data-contribution-id={contributionId}
        data-module-url={moduleUrl}
      >
        Loading trusted-local plugin UI...
      </div>
    );
  }

  return (
    <TrustedLocalContributionErrorBoundary
      key={`${moduleUrl}:${componentKey}`}
      contributionId={contributionId}
      pluginId={pluginId}
      moduleUrl={moduleUrl}
    >
      <div
        className="trusted-local-plugin-component"
        data-testid={testId}
        data-plugin-id={pluginId}
        data-contribution-id={contributionId}
        data-component-key={componentKey}
        data-module-url={moduleUrl}
        data-error-boundary="trustedLocalContribution"
      >
        <Component
          React={React}
          ctx={ctx ?? null}
          contributionId={contributionId}
          pluginId={pluginId}
        />
      </div>
    </TrustedLocalContributionErrorBoundary>
  );
}
