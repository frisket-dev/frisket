import { Button } from '../Button';
import { TextInput } from '../FormControls';
import { PanelSelect } from '../PanelSelect';
import type { FeedInterval } from '../../api/open';
import { INTERVALS, FEED_KINDS, selectedKind, type KnownFeedKind, type FeedFormState } from './feedConfig';
import { FeedConfigFields } from './FeedConfigFields';
import type { FeedKindFormFrame } from './feedFrames';
import type { BusyFeed } from './feedState';
import type { FeedPanelActions } from './useFeedsPanelState';
import '../FeedsPanel.css';

interface FeedEditorFormProps {
  actions: FeedPanelActions;
  busy: BusyFeed;
  form: FeedFormState;
  formFrame?: FeedKindFormFrame;
}

export function FeedEditorForm({ actions, busy, form, formFrame }: FeedEditorFormProps) {
  const kind = selectedKind(form);
  const Frame = formFrame;

  const formElement = (
    <form className="content-stack" data-testid="feed-form" onSubmit={actions.submit}>
      <div className="feeds-form-row">
        <PanelSelect
          className="form-input"
          aria-label="Feed type"
          data-testid="feed-kind"
          value={form.kind}
          onChange={(e) => actions.updateForm({ kind: e.target.value as KnownFeedKind })}
        >
          {FEED_KINDS.map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </PanelSelect>
      </div>
      <TextInput
        placeholder="Feed name"
        aria-label="Feed name"
        data-testid="feed-name"
        value={form.name}
        onChange={(e) => actions.updateForm({ name: e.target.value })}
      />
      <label className="form-label" htmlFor="source-url-input">
        {kind.urlLabel}
      </label>
      <TextInput
        id="source-url-input"

        placeholder={kind.placeholder}
        aria-label={kind.urlLabel}
        data-testid="feed-url"
        value={form.url}
        onChange={(e) => actions.updateForm({ url: e.target.value })}
      />

      <FeedConfigFields form={form} onChange={actions.updateForm} />
      {form.kind === 'courtlistener_docket' && <div className="panel-item-meta">RECAP PDFs stay link-only. Runtime tokens are not stored in feed config.</div>}

      <PanelSelect
        className="form-input"
        aria-label="Check schedule"
        data-testid="feed-interval"
        value={form.cadence}
        onChange={(e) => actions.updateForm({ cadence: e.target.value as FeedInterval })}
      >
        {INTERVALS.map((i) => (
          <option key={i.value} value={i.value}>{i.label}</option>
        ))}
      </PanelSelect>
      <div className="control-row" data-align="end">
        <Button
          type="button"

          onClick={() => actions.setAdding(false)}
        >
          Cancel
        </Button>
        <Button
          type="submit"
          className="btn-primary"
          data-testid="feed-create"
          disabled={busy !== null || !form.name.trim() || !form.url.trim()}
        >
          {busy !== null
            ? (form.editingId == null ? 'Adding...' : 'Saving...')
            : (form.editingId == null ? 'Add' : 'Save')}
        </Button>
      </div>
    </form>
  );

  if (!Frame) return formElement;
  return <Frame sourceKind={form.kind}>{formElement}</Frame>;
}
