import '../components/PanelPrimitives.css';
import './DiagnosticReport.css';
import { Button } from '../components/Button';
import { TextArea } from '../components/FormControls';
// Hosted-tier diagnostic report surface: the "Report issue" button + popover
// that bundles a diagnostic report, and the reader for recent client-side error
// ids captured on window. Self-contained; rendered only in hosted mode.
import { useState } from 'react';
import { Bug, Send } from 'lucide-react';
import { WindowCloseButton } from '../components/WindowCloseButton';
import { createDiagnosticBundle } from '../api/open';
import { browserRouteContext } from '../routeContext';

function recentClientErrorIds(): number[] {
  const globalWindow = window as typeof window & {
    __FRISKET_RECENT_CLIENT_ERROR_IDS__?: unknown;
  };
  const raw = globalWindow.__FRISKET_RECENT_CLIENT_ERROR_IDS__;
  if (!Array.isArray(raw)) return [];
  const ids: number[] = [];
  const seenIds = new Set<number>();
  for (const item of raw) {
    const value = typeof item === 'number' ? item : Number(item);
    if (!Number.isInteger(value) || value <= 0 || seenIds.has(value)) continue;
    seenIds.add(value);
    ids.push(value);
    if (ids.length >= 10) break;
  }
  return ids;
}

interface DiagnosticReportState {
  open: boolean;
  busy: boolean;
  sentId: number | null;
  error: string | null;
}

export function DiagnosticReportButton() {
  const [description, setDescription] = useState('');
  const [report, setReport] = useState<DiagnosticReportState>({
    open: false,
    busy: false,
    sentId: null,
    error: null,
  });

  const submit = () => {
    const message = description.trim();
    if (!message || message.length > 1000) return;
    setReport((state) => ({ ...state, busy: true, error: null }));
    createDiagnosticBundle({
      message,
      route: window.location.pathname,
      include_raw_values: false,
      context: browserRouteContext(),
      recent_client_error_ids: recentClientErrorIds(),
    })
      .then((resp) => {
        setDescription('');
        setReport((state) => ({
          ...state,
          sentId: resp.report_id,
          open: false,
        }));
      })
      .catch((e: unknown) => {
        setReport((state) => ({
          ...state,
          error: e instanceof Error ? e.message : String(e),
        }));
      })
      .finally(() => setReport((state) => ({ ...state, busy: false })));
  };

  return (
    <div className="diagnostic-report">
      <button
        type="button"
        className="status-btn"
        data-testid="report-issue-button"
        title="Report issue"
        aria-expanded={report.open}
        onClick={() => {
          setReport((state) => ({
            ...state,
            open: !state.open,
            sentId: null,
          }));
        }}
      >
        <Bug size={15} />
        <span>Report issue</span>
      </button>
      {report.open && (
        <div className="diagnostic-popover" data-testid="diagnostic-popover">
          <div className="diagnostic-head">
            <strong>Report issue</strong>
            <WindowCloseButton
              size="compact"
              label="Close report issue"
              onClick={() => setReport((state) => ({ ...state, open: false }))}
            />
          </div>
          {report.error && <div className="panel-error" role="alert" data-testid="diagnostic-error">{report.error}</div>}
          <label className="field-group form-field">
            <span className="field-labels">What went wrong?</span>
            <TextArea
              data-testid="diagnostic-description"
              value={description}
              rows={4}
              required
              maxLength={1000}
              disabled={report.busy}
              placeholder="Briefly describe what you expected and what happened."
              onChange={(event) => setDescription(event.currentTarget.value)}
            />
          </label>
          <Button
            type="button"
            className="diagnostic-send"
            data-testid="diagnostic-submit"
            disabled={report.busy || !description.trim() || description.trim().length > 1000}
            onClick={submit}
          >
            <Send size={14} />
            {report.busy ? 'Sending…' : 'Send report'}
          </Button>
        </div>
      )}
      {report.sentId !== null && (
        <div className="diagnostic-sent" role="status" data-testid="diagnostic-sent">
          Report sent #{report.sentId}
        </div>
      )}
    </div>
  );
}
