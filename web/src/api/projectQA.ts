import { httpContract } from './httpContract';
import type {
  HttpAskEventsPage, HttpAskReport, HttpAskThread, HttpAskThreadCreate,
  HttpAskThreadDetail, HttpAskThreadUpdate, HttpAskTurn, HttpAskTurnRequest,
} from '../generated/openHttpContracts';

export type AskThread = HttpAskThread;
export type AskScope = AskThread['scope'];
export type AskEvent = HttpAskEventsPage['events'][number];
export type AskTurn = HttpAskTurn;
export type AskThreadCreate = HttpAskThreadCreate;
export type AskThreadUpdate = HttpAskThreadUpdate;
export type AskTurnRequest = HttpAskTurnRequest;

export interface ProjectQAApi {
  list(signal?: AbortSignal): Promise<AskThread[]>;
  create(body: AskThreadCreate, signal?: AbortSignal): Promise<AskThread>;
  detail(threadId: string, signal?: AbortSignal): Promise<HttpAskThreadDetail>;
  update(threadId: string, body: AskThreadUpdate, signal?: AbortSignal): Promise<AskThread>;
  delete(threadId: string, signal?: AbortSignal): Promise<void>;
  submit(threadId: string, body: AskTurnRequest, signal?: AbortSignal): Promise<AskTurn>;
  events(threadId: string, query: { after?: number; before?: number; limit?: number }, signal?: AbortSignal): Promise<HttpAskEventsPage>;
  stop(threadId: string, turnId: string, signal?: AbortSignal): Promise<AskTurn>;
  report(threadId: string, signal?: AbortSignal): Promise<HttpAskReport>;
}

export function createProjectQAApi(projectId: () => string, errorFactory: (status: number, payload: unknown) => Error): ProjectQAApi {
  const path = (thread_id: string) => ({ pid: projectId(), thread_id });
  return {
    list: (signal) => httpContract('tenant.qa_threads.get', {
      pathParams: { pid: projectId() }, query: {}, signal, errorFactory,
    }),
    create: (body, signal) => httpContract('tenant.qa_create_thread.post', {
      pathParams: { pid: projectId() }, query: {}, body, signal, errorFactory,
    }),
    detail: (threadId, signal) => httpContract('tenant.qa_thread.get', {
      pathParams: path(threadId), query: {}, signal, errorFactory,
    }),
    update: (threadId, body, signal) => httpContract('tenant.qa_update_thread.patch', {
      pathParams: path(threadId), query: {}, body, signal, errorFactory,
    }),
    delete: async (threadId, signal) => { await httpContract('tenant.qa_delete_thread.delete', {
      pathParams: path(threadId), query: {}, signal, errorFactory,
    }); },
    submit: (threadId, body, signal) => httpContract('tenant.qa_submit_turn.post', {
      pathParams: path(threadId), query: {}, body, signal, errorFactory,
    }),
    events: (threadId, query, signal) => httpContract('tenant.qa_events.get', {
      pathParams: path(threadId), query, signal, errorFactory,
    }),
    stop: (threadId, turn_id, signal) => httpContract('tenant.qa_stop_turn.post', {
      pathParams: { ...path(threadId), turn_id }, query: {}, signal, errorFactory,
    }),
    report: (threadId, signal) => httpContract('tenant.qa_report.get', {
      pathParams: path(threadId), query: {}, signal, errorFactory,
    }),
  };
}
