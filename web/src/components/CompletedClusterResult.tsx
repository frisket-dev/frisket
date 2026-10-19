import { Button } from './Button';
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
    <Button type="button" className="btn-primary" onClick={() => {
      onCreate(receiptId);
      jobs.dismissCompletedClusterResult();
    }}>Create entity table</Button>
    <Button type="button" onClick={jobs.dismissCompletedClusterResult}
      aria-label="Dismiss clustering result">Dismiss</Button>
  </aside>;
}
