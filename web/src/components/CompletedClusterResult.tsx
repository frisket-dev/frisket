import { useJobsHandle } from '../bind/useJobsHandle';
import { useSelector } from '../bind/useSelector';
import './completed-cluster-result.css';

export function CompletedClusterResult({ onCreate }: { onCreate(receiptId: string): void }) {
  const jobs = useJobsHandle();
  const receiptId = useSelector(jobs.store, (state) => state.completedClusterReceiptId);
  if (!receiptId) return null;
  return <aside className="completed-cluster-result" data-testid="completed-cluster-result"
    aria-label="Completed clustering result">
    <span role="status">Clustered values saved.</span>
    <button type="button" className="btn btn-primary" onClick={() => {
      onCreate(receiptId);
      jobs.dismissCompletedClusterResult();
    }}>Create entity table</button>
    <button type="button" className="mini-btn" onClick={jobs.dismissCompletedClusterResult}
      aria-label="Dismiss clustering result">Dismiss</button>
  </aside>;
}
