import { ArrowRight, FolderPlus } from 'lucide-react';
import styles from './HomeSampleHero.module.css';
import type { HomeOpenOptions } from './HomeScreen';

export function HomeSampleHero({
  busy,
  onOpenSample,
  onNewProject,
}: {
  busy: boolean;
  onOpenSample(options?: HomeOpenOptions): void;
  onNewProject(): void;
}) {
  return (
    <section className={styles.layout} data-testid="home-sample-hero" aria-label="Get started">
      <div className={styles.hero}>
        <span className={styles.eyebrow}>Start here</span>
        <h2 className={styles.heading}>Open the sample project</h2>
        <p className={styles.copy}>
          Sample data is ready to explore, with guided walkthroughs when you want them.
        </p>
        <div className={styles.actions}>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => onOpenSample()}
            disabled={busy}
          >
            {busy ? 'Setting up the sample…' : <>Open the sample project <ArrowRight size={13} /></>}
          </button>
          <button
            type="button"
            className="btn"
            onClick={() => onOpenSample({ openGuide: true })}
            disabled={busy}
          >
            See the walkthroughs
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
