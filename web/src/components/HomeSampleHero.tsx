import { ArrowRight, FolderPlus } from 'lucide-react';
import styles from './HomeSampleHero.module.css';

export function HomeSampleHero({
  busy,
  onOpenSample,
  onNewProject,
}: {
  busy: boolean;
  onOpenSample(): void;
  onNewProject(): void;
}) {
  return (
    <section className={styles.layout} data-testid="home-sample-hero" aria-label="Get started">
      <div className={styles.hero}>
        <span className={styles.eyebrow}>Start here</span>
        <h2 className={styles.heading}>Open the sample project</h2>
        <p className={styles.copy}>
          Explore sample data with guided walkthroughs.
        </p>
        <div className={styles.actions}>
          <button
            type="button"
            className="btn btn-primary"
            data-testid="try-sample-project"
            onClick={onOpenSample}
            disabled={busy}
          >
            {busy ? 'Setting up the sample…' : <>Try the sample project <ArrowRight size={13} /></>}
          </button>
        </div>
      </div>
      <button type="button" className={styles.newProject} onClick={onNewProject}>
        <FolderPlus size={20} />
        <span>New project</span>
        <span className={styles.newProjectHint}>Import data or start empty</span>
      </button>
    </section>
  );
}
