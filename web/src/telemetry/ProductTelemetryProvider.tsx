import '../components/FormControls.css';
import { Button } from '../components/Button';
import { Modal } from '../components/Modal';
import { useEffect, useState, type ReactNode } from 'react';
import { getRuntimeConfig } from '../api/open';
import { TelemetryDisclosureReadyContext } from './disclosureReady';
import {
  configureProductTelemetry,
  sendAppOpened,
  setTelemetryPreference,
  telemetryPreference,
} from './productTelemetry';

export function ProductTelemetryProvider({ children }: { children: ReactNode }) {
  const [available, setAvailable] = useState(false);
  const [resolved, setResolved] = useState(false);
  const [choice, setChoice] = useState(telemetryPreference);
  const [checked, setChecked] = useState(true);

  useEffect(() => {
    let active = true;
    getRuntimeConfig().then((config) => {
      if (!active) return;
      configureProductTelemetry(config.product_telemetry_available);
      setAvailable(config.product_telemetry_available);
      setResolved(true);
      if (config.product_telemetry_available && telemetryPreference() === 'enabled') {
        sendAppOpened();
      }
    }).catch(() => {
      if (active) setResolved(true);
    });
    return () => { active = false; };
  }, []);

  const continueToFrisket = () => {
    setTelemetryPreference(checked);
    setChoice(checked ? 'enabled' : 'disabled');
    if (checked) sendAppOpened();
  };

  return (
    <TelemetryDisclosureReadyContext.Provider value={resolved && (!available || choice !== 'unanswered')}>
      {children}
      {available && choice === 'unanswered' ? (
        <Modal title="Help improve frisket" testId="product-telemetry-disclosure">
            <p>
              Send pseudonymous product telemetry: features used, whether imports and actions work,
              and coarse size and timing ranges.
            </p>
            <p className="muted">
              Product telemetry does not include your data, filenames, project or column names,
              prompts, results, URLs, or error messages. A random browser identifier resets on the
              first of every month. TelemetryDeck processes the events for frisket.
            </p>
            <label className="control-row">
              <input type="checkbox" checked={checked} onChange={(event) => setChecked(event.currentTarget.checked)} />
              <span>Send product telemetry</span>
            </label>
            <p className="muted">You can change this anytime in Settings → Personal → Privacy. Sensitive projects send no project telemetry.</p>
            <div className="form-actions">
              <Button type="button" className="btn-primary" onClick={continueToFrisket}>Continue</Button>
            </div>
        </Modal>
      ) : null}
    </TelemetryDisclosureReadyContext.Provider>
  );
}
