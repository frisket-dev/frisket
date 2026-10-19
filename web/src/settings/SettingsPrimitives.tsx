import './SettingsPrimitives.css';
import type { ReactNode } from 'react';
import { PanelError, PanelLoading, PanelNotice } from '../components/PanelPrimitives';

export function InlineStatus({
  error,
  notice,
  loading,
}: {
  error?: string | null;
  notice?: string | null;
  loading?: boolean;
}) {
  if (loading) return <PanelLoading label="Loading..." />;
  if (error) return <PanelError>{error}</PanelError>;
  if (notice) return <PanelNotice>{notice}</PanelNotice>;
  return null;
}

export function SettingsTable({
  headers,
  rows,
  empty,
}: {
  headers: string[];
  rows: Array<Array<ReactNode>>;
  empty: string;
}) {
  if (rows.length === 0) return <div className="panel-empty">{empty}</div>;
  return (
    <div className="settings-table-wrap">
      <table className="settings-table">
        <thead>
          <tr>{headers.map((header) => <th key={header}>{header}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((row, idx, all) => {
            const content = row.map(String).join('|');
            // Content-based key; duplicate rows (possible in spend tables)
            // get an occurrence suffix instead of a bare array index.
            const occurrence = all
              .slice(0, idx)
              .filter((other) => other.map(String).join('|') === content).length;
            return (
            <tr key={occurrence ? `${content}#${occurrence}` : content}>
              {row.map((cell, cellIndex) => <td key={cellIndex}>{cell}</td>)}
            </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
