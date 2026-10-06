import styles from './ExtractView.module.css';

export function ExtractToolHelp() {
  return <div className={styles.toolHelp}>
    <p><strong>Select</strong> Move, resize, or delete boxes on the example document.</p>
    <p><strong>Key / value</strong> Draw the label, then draw its value. The label can be found elsewhere in a document.</p>
    <p><strong>Value only</strong> Draw one value at a fixed page and position, then name its column in Fields.</p>
    <p><strong>Repeated section</strong> Draw the first record, then all remaining records. Add key/value fields inside the first record.</p>
    <p><strong>Ignore region</strong> Draw a header or footer to exclude on every page.</p>
  </div>;
}
