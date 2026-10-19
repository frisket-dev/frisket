import './DiagnosticsSettings.css';
import { Button } from '../components/Button';
import { CheckCircle2, Info, RefreshCw, XCircle } from 'lucide-react';
import { readSettingsProjectContext } from './settingsProjectContext';
import { fetchDiagnostics, type DiagnosticsReport } from '../api/diagnostics';
import { useLoader } from './settingsSupport';
import { InlineStatus } from './SettingsPrimitives';

// Diagnostics section: the self-probe GET /api/diagnose canonical home,
// extracted from the retired DiagnosePanel.tsx <dialog>. Both the error-toast
// "Open Diagnose" link and the action panel's engine-availability disclosure
// now land here via chromeStore's openDiagnosePanel()
// (App.tsx / state/chromeStore.ts), which navigates to this section instead of
// opening a modal.
//
// core/info are typed `unknown`, not a shape, deliberately: the response is
// only type-ASSERTED by fetchDiagnostics below, never runtime-validated
// (src/frisket/operability/diagnostics.py's INFO wrapper returns fn() without validating
// its shape either), so a probe that diverges from the assumed shape must
// degrade gracefully here instead of throwing into the root error boundary
// (entries/mount.tsx).
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Object.entries over a value that MUST be a plain object to iterate safely
 *  (Object.entries itself throws on null/undefined, and silently no-ops on
 *  other primitives) — an absent/null/malformed core or info collection
 *  renders as an empty list instead of crashing. */
function safeEntries(value: unknown): Array<[string, unknown]> {
  return isRecord(value) ? Object.entries(value) : [];
}

function diagnosticsCoreLine(value: unknown): string {
  if (!isRecord(value)) return value == null ? '—' : JSON.stringify(value);
  if (value.ok) return typeof value.detail === 'string' ? value.detail : 'ok';
  return typeof value.error === 'string' ? value.error : 'failed';
}

function diagnosticsInfoLine(value: unknown): string {
  if (isRecord(value)) {
    return typeof value.summary === 'string' ? value.summary : JSON.stringify(value);
  }
  return value == null ? '—' : String(value);
}

type DiagnosticRowStatus = 'ok' | 'failed' | 'info' | 'unknown';

function diagnosticsCoreStatus(value: unknown): DiagnosticRowStatus {
  if (!isRecord(value)) return 'unknown';
  if (value.ok === true) return 'ok';
  if (value.ok === false) return 'failed';
  return 'unknown';
}

function diagnosticLabel(key: string, info = false): string {
  const raw = info ? DIAGNOSTICS_INFO_LABELS[key] : undefined;
  const label = raw ?? key.replace(/_/g, ' ');
  return label.charAt(0).toUpperCase() + label.slice(1);
}

function DiagnosticStatus({ status }: { status: DiagnosticRowStatus }) {
  const copy = status === 'ok'
    ? 'OK'
    : status === 'failed'
      ? 'Failed'
      : status === 'info'
        ? 'Info'
        : 'Unknown';
  const Icon = status === 'ok'
    ? CheckCircle2
    : status === 'failed'
      ? XCircle
      : Info;
  return (
    <span className={`diagnose-status diagnose-status-${status}`} data-status={status}>
      <Icon size={13} aria-hidden />
      {copy}
    </span>
  );
}

const DIAGNOSTICS_INFO_LABELS: Record<string, string> = {
  model_providers: 'Model providers',
  local_model_endpoints: 'Local model endpoints',
  local_engines: 'Local engines',
  models_sidecar: 'Models sidecar',
  media_toolbelt: 'Media toolbelt',
  static_assets: 'Static assets',
  // Plugin HEALTH (counts), as opposed to individual load/runtime FAILURES,
  // which route into the workbench Errors dock instead (dockJobSummary.ts's
  // derivePluginErrorJobs). Confirmed shape: an INFO entry keyed "plugins" of
  // exactly `{available, installed, active, failed, failed_names, summary}`,
  // `summary` a display string, always present in both success and
  // failure/exception cases.
  // diagnosticsInfoLine reads `summary` when present but degrades gracefully
  // (isRecord/JSON.stringify/'—') for any other shape.
  plugins: 'Plugin runtime health',
};

/** Self-probe fetch, hoisted to module scope so the network call lives in the
 *  data layer rather than inside an effect (react-doctor no-fetch-in-effect;
 *  matches useLoader's own load-fn-at-module-scope convention).
 *
 *  `/api/diagnose` accepts an OPTIONAL `project_id` query param
 *  (src/frisket/server/routes/diagnose.py)
 *  that populates the plugin-runtime-health probe for that project. This
 *  section has no project prop of its own, so it reads the SAME
 *  settingsProjectContext.ts store the back-link uses (written by
 *  chromeStore's openDiagnosePanel / AccountMenu's goPersonal before
 *  navigating here) — when a project is in context its id rides along;
 *  otherwise the probe degrades to "no project in scope", same as omitting
 *  the param entirely. */
function loadDiagnostics(): Promise<DiagnosticsReport> {
  const projectId = readSettingsProjectContext()?.id;
  // The project rides on the PATH, never a query parameter: the server's role
  // ladder only resolves a project from {pid}, so a project named anywhere else
  // is invisible to the gate (diagnose-project-on-the-path-v1).
  return fetchDiagnostics(projectId);
}

/** Reachable from LLM-run error states: the same self-probe `frisket doctor`
 *  prints to a terminal, in-app. See src/frisket/operability/diagnostics.py — this section
 *  and the CLI compute identical facts from one shared module. Exported for
 *  component tests, mirroring WorkspaceAiProvidersSettings' own precedent. */
export function DiagnosticsSettings() {
  const report = useLoader<DiagnosticsReport>(() => loadDiagnostics());
  // useLoader's 'fail' action keeps the previous
  // `data` around (so a transient refresh failure doesn't blank out an
  // otherwise-fine report) — but rendering that stale report under an
  // unqualified "Healthy" status, with only a separate error banner above it,
  // reads as "this is the CURRENT state" when it's actually last-known-good.
  // A failed refresh with no prior report at all still degrades to the bare
  // InlineStatus error banner (report.data is null, the block below doesn't
  // render) — this only relabels the case where stale data is ALSO on screen.
  const stale = Boolean(report.error) && !report.loading && report.data != null;
  return (
    <div className="content-stack" data-testid="settings-diagnostics">
      <InlineStatus error={report.error} loading={report.loading} />
      {report.data && (
        <div className="diagnose-panel-body" data-testid="diagnose-panel-body">
          {stale && (
            <p className="panel-notice" data-tone="neutral" data-testid="diagnose-panel-stale-note">
              Refresh failed — showing the last successful check.
            </p>
          )}
          <div className="diagnose-panel-status" data-testid="diagnose-panel-status">
            {stale ? 'Last known: ' : ''}{report.data.healthy ? 'Healthy' : 'Unhealthy'}
          </div>
          <div className="diagnose-panel-table-wrap">
            <table className="diagnose-panel-table" data-testid="diagnose-panel-table">
              <thead>
                <tr>
                  <th scope="col">Check</th>
                  <th scope="col">Status</th>
                  <th scope="col">Detail</th>
                </tr>
              </thead>
              <tbody>
                {safeEntries(report.data.core).map(([key, value]) => (
                  <tr
                    key={`core:${key}`}
                    className={`diagnose-row diagnose-row-${diagnosticsCoreStatus(value)}`}
                    data-testid={`diagnose-row-${key}`}
                  >
                    <th scope="row">{diagnosticLabel(key)}</th>
                    <td><DiagnosticStatus status={diagnosticsCoreStatus(value)} /></td>
                    <td>{diagnosticsCoreLine(value)}</td>
                  </tr>
                ))}
                {safeEntries(report.data.info).map(([key, value]) => (
                  <tr
                    key={`info:${key}`}
                    className="diagnose-row diagnose-row-info"
                    data-testid={`diagnose-row-${key}`}
                  >
                    <th scope="row">{diagnosticLabel(key, true)}</th>
                    <td><DiagnosticStatus status="info" /></td>
                    <td>{diagnosticsInfoLine(value)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
      <div className="control-row">
        <Button

          type="button"
          data-testid="settings-diagnostics-refresh"
          disabled={report.loading}
          onClick={() => void report.reload()}
        >
          <RefreshCw size={14} /> Refresh
        </Button>
      </div>
    </div>
  );
}
