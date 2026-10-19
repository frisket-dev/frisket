import type { ReviewValueExcerpt } from '../../api/types';
import { formatNumberDisplay } from '../../format';
import styles from './ReviewValue.module.css';

/** Plain text only: a bounded excerpt cannot represent parsed result items. */
export function ReviewExcerpt({ excerpt }: { excerpt: ReviewValueExcerpt }) {
  return <div className={styles.excerpt} data-testid="review-excerpt">
    <p className={styles.excerptNotice}>Excerpt · {formatNumberDisplay(excerpt.totalBytes, 'filesize')} total</p>
    <pre className={styles.excerptText}>{excerpt.preview}</pre>
    {excerpt.downloadHref && <a className="btn" href={excerpt.downloadHref} download>Download full value</a>}
  </div>;
}
