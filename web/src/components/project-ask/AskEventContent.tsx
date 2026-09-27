import { useState } from 'react';
import { ChartColumnIncreasing, ChevronRight, Copy } from 'lucide-react';
import type { AskCitation, AskEvent } from '../../api/projectQA';
import type { ActionCatalogEntry, GeneratedActionDraft } from '../../api/types';
import { askActionProposal, type AskActionProposal } from './actionProposal';
import { AskAnswer } from './AskAnswer';
import { useAskCitation } from './useAskCitation';

export function AskEventContent({ event, onInspectProposal, onOpenSource, actionProposals = [], actionCatalog = [], onOpenAction }: {
  event: AskEvent;
  onOpenSource(citation: AskCitation): void;
  onInspectProposal(title: string, spec: GeneratedActionDraft): void;
  actionProposals?: readonly AskActionProposal[];
  actionCatalog?: readonly ActionCatalogEntry[];
  onOpenAction?(actionId: string): void;
}) {
  const payload = event.payload;
  const [copyStatus, setCopyStatus] = useState<string | null>(null);
  const { source, sourceError, openSource } = useAskCitation(event.thread_id, onOpenSource);
  async function copyDebug(text: string) {
    try { await navigator.clipboard.writeText(text); setCopyStatus('Copied'); }
    catch { setCopyStatus('Could not copy. Please try again.'); }
  }
  if (event.kind === 'action_proposal') {
    const proposal = askActionProposal(event);
    if (!proposal) return null;
    return <div className="ask-proposal"><strong>{proposal.title}</strong><p>Review the settings before running this action.</p><button type="button" className="btn" onClick={() => onInspectProposal(proposal.title, proposal.spec)}>Open action</button></div>;
  }
  if (event.kind === 'result_suggestion') {
    const title = String(payload.title ?? 'Matching records');
    const total = typeof payload.total === 'number' ? payload.total.toLocaleString() : null;
    const sheetName = typeof payload.sheet_name === 'string' && payload.sheet_name.trim() ? payload.sheet_name : title;
    const open = () => { if (typeof payload.citation_id === 'string') openSource(payload.citation_id); };
    return <div className="ask-analysis"><button type="button" className="ask-analysis-summary" onClick={open}><ChartColumnIncreasing size={15} aria-hidden /><span>{total ? `${total} ${Number(payload.total) === 1 ? 'row' : 'rows'} in ${sheetName}` : sheetName}</span><ChevronRight size={15} aria-hidden /></button>{source?.message && source.status !== 'current' && <p>{source.message}</p>}{sourceError && <p role="alert">{sourceError}</p>}</div>;
  }
  if (event.kind === 'question') return <div className="ask-question">{String(payload.question ?? '')}</div>;
  if (event.kind === 'answer' || event.kind === 'assistant') return <AskAnswer event={event} actionProposals={actionProposals} actionCatalog={actionCatalog} onOpenAction={onOpenAction} onInspectProposal={onInspectProposal} onOpenSource={onOpenSource} />;
  if (event.kind === 'status' && ['stopped', 'interrupted', 'failed'].includes(String(payload.status))) {
    const rawDiagnostic = payload.diagnostic;
    const diagnostic = rawDiagnostic !== null && typeof rawDiagnostic === 'object' && !Array.isArray(rawDiagnostic) ? rawDiagnostic as Record<string, unknown> : null;
    const code = diagnostic?.code === 'invalid_model_response' || diagnostic?.code === 'internal_error' ? diagnostic.code : null;
    const reasonLabels: Record<string, string> = { tool_call_invalid: 'Invalid tool arguments', citation_invalid: 'Invalid source citation', final_result_invalid: 'Invalid answer format', output_failure: 'Model output was not accepted' };
    const reason = typeof diagnostic?.reason === 'string' ? reasonLabels[diagnostic.reason] : undefined;
    const reference = typeof diagnostic?.reference === 'string' ? diagnostic.reference : null;
    const tool = typeof diagnostic?.tool === 'string' ? diagnostic.tool : null;
    const hasDetails = payload.status === 'failed' && !!code && !!reference;
    const debugText = [code && `Code: ${code}`, reason && `Reason: ${reason}`, reference && `Reference: ${reference}`, tool && `Tool: ${tool}`].filter((line): line is string => !!line).join('\n');
    const errorSummary = typeof payload.error_summary === 'string' && payload.error_summary.trim() ? payload.error_summary : null;
    const summary = payload.status === 'stopped' ? 'Stopped. The work above is saved.' : payload.status === 'interrupted' ? 'Interrupted. Send a follow-up to continue.'
      : errorSummary ?? (code === 'invalid_model_response' ? 'The selected model returned an unusable response. Try a more specific question or another model.' : 'Ask could not complete this question. Please try again.');
    return <div className="ask-status ask-failure-status"><p>{summary}</p>{hasDetails && <details className="ask-failure-details"><summary>Details</summary><dl><div><dt>Code</dt><dd>{code}</dd></div>{reason && <div><dt>Reason</dt><dd>{reason}</dd></div>}<div><dt>Reference</dt><dd>{reference}</dd></div>{tool && <div><dt>Tool</dt><dd>{tool}</dd></div>}</dl><button type="button" aria-label="Copy failure details" title="Copy failure details" onClick={() => void copyDebug(debugText)}><Copy size={13} aria-hidden /></button>{copyStatus && <span role="status">{copyStatus}</span>}</details>}</div>;
  }
  return null;
}
