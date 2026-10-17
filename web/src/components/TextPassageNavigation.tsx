import { useState } from 'react';
import type { TextPassageWindow } from '../api/types';

/** Navigation addresses absolute codepoints; renderers slice only local UTF-16. */
export function TextPassageNavigation({ window, disabled, onNavigate }: {
  window: TextPassageWindow; disabled?: boolean; onNavigate(start: number): void;
}) {
  const [character, setCharacter] = useState('');
  return <nav aria-label="Text passage navigation" data-testid="text-passage-navigation">
    <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 8 }}>
      <button type="button" className="mini-btn" disabled={disabled || window.previous_start === null}
        onClick={() => window.previous_start !== null && onNavigate(window.previous_start)}>Previous passage</button>
      <span className="muted" role="status">Characters {window.total_characters ? window.start + 1 : 0}–{window.end} of {window.total_characters.toLocaleString()}</span>
      <button type="button" className="mini-btn" disabled={disabled || window.next_start === null}
        onClick={() => window.next_start !== null && onNavigate(window.next_start)}>Next passage</button>
    </div>
    <form style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBlock: 8 }} onSubmit={(event) => {
      event.preventDefault();
      const value = Number(character);
      if (Number.isInteger(value) && value >= 1 && value <= window.total_characters) onNavigate(value - 1);
    }}>
      <label>Character <input aria-label="Go to source character" type="number" min={1} max={window.total_characters}
        style={{ width: '9ch' }} value={character} onChange={(event) => setCharacter(event.target.value)} /></label>
      <button type="submit" className="mini-btn" disabled={disabled || !character}>Go</button>
    </form>
  </nav>;
}
