import { type FormEvent, useState } from 'react';
import { Plus, Trash2 } from 'lucide-react';
import { ApiError } from '../api/contractErrors';
import {
  listApprovedEndpoints,
  replaceApprovedEndpoints,
  type ApprovedEndpoint,
  type ApprovedEndpointList,
  type ApprovedEndpointsScope,
} from '../api/approvedEndpoints';
import { Button } from '../components/Button';
import { FormField, TextInput } from '../components/FormControls';
import { PanelNotice } from '../components/PanelPrimitives';
import { errorMessage } from '../hooks/errorMessage';
import { useLoader } from '../hooks/useLoader';
import { InlineStatus, SettingsTable } from './SettingsPrimitives';

function endpointKey(endpoint: ApprovedEndpoint): string {
  return `${endpoint.host}:${endpoint.port ?? ''}`;
}

/** Manage approved endpoints: the destinations the Approved services setting
 *  allows, added explicitly. Team organization members read the list; only
 *  organization owners replace it, which the server decides. */
export function ApprovedEndpointsSettings({ scope }: { scope: ApprovedEndpointsScope }) {
  const list = useLoader<ApprovedEndpointList>(() => listApprovedEndpoints(scope), [scope]);
  const [host, setHost] = useState('');
  const [port, setPort] = useState('');
  const [label, setLabel] = useState('');
  const [busy, setBusy] = useState(false);
  const [refused, setRefused] = useState(false);
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const endpoints = list.data?.endpoints ?? [];
  // The server says whether this reader may change the list; a refusal after
  // loading (a role changed meanwhile) also ends editing.
  const readOnly = list.data !== null && (!list.data.editable || refused);

  const replace = async (next: ApprovedEndpoint[], done: string) => {
    setBusy(true);
    setMutationError(null);
    setNotice(null);
    try {
      list.setData({ endpoints: await replaceApprovedEndpoints(scope, next), editable: true });
      setNotice(done);
      return true;
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 403) {
        setRefused(true);
      } else {
        setMutationError(errorMessage(caught));
      }
      return false;
    } finally {
      setBusy(false);
    }
  };

  const trimmedPort = port.trim();
  const portNumber = trimmedPort === '' ? null : Number(trimmedPort);
  const portValid = portNumber === null || (Number.isInteger(portNumber) && portNumber >= 1 && portNumber <= 65535);
  const add = async (event: FormEvent) => {
    event.preventDefault();
    if (!host.trim() || !portValid || busy) return;
    const added = await replace(
      [...endpoints, { host: host.trim(), port: portNumber, label: label.trim() }],
      'Endpoint added',
    );
    if (added) {
      setHost('');
      setPort('');
      setLabel('');
    }
  };

  return (
    <div className="content-stack" data-testid="approved-endpoints-settings">
      <InlineStatus error={list.error ?? mutationError} loading={list.loading} notice={notice} />
      <p className="form-hint">
        Projects set to Approved services may also use these destinations. Each entry matches a
        host, and a port when one is given. A model server added under AI Providers is approved by
        being added and is not listed here. Nothing else is added automatically.
      </p>
      {readOnly && (
        <PanelNotice>Only organization owners can change approved endpoints.</PanelNotice>
      )}
      <SettingsTable
        headers={['Host', 'Port', 'Label', '']}
        rows={endpoints.map((endpoint, index) => [
          endpoint.host,
          endpoint.port ?? 'Any',
          endpoint.label || '-',
          readOnly ? null : (
            <button
              key={endpointKey(endpoint)}
              className="icon-btn"
              type="button"
              aria-label={`Remove ${endpoint.host}`}
              title={`Remove ${endpoint.host}`}
              data-testid={`approved-endpoint-remove-${index}`}
              disabled={busy}
              onClick={() => void replace(endpoints.filter((_, at) => at !== index), 'Endpoint removed')}
            >
              <Trash2 size={14} />
            </button>
          ),
        ])}
        empty="No approved endpoints"
      />
      {list.data !== null && !readOnly && (
        <form className="form-inline" onSubmit={(event) => void add(event)} data-testid="approved-endpoint-add-form">
          <FormField label="Host">
            <TextInput
              data-testid="approved-endpoint-host"
              placeholder="models.example.org"
              value={host}
              disabled={busy}
              onChange={(event) => setHost(event.currentTarget.value)}
            />
          </FormField>
          <FormField label="Port (optional)" hint={portValid ? undefined : 'A port is a number from 1 to 65535.'}>
            <TextInput
              data-testid="approved-endpoint-port"
              inputMode="numeric"
              value={port}
              disabled={busy}
              aria-invalid={!portValid || undefined}
              onChange={(event) => setPort(event.currentTarget.value)}
            />
          </FormField>
          <FormField label="Label (optional)">
            <TextInput
              data-testid="approved-endpoint-label"
              value={label}
              disabled={busy}
              onChange={(event) => setLabel(event.currentTarget.value)}
            />
          </FormField>
          <Button
            type="submit"
            className="btn-primary"
            data-testid="approved-endpoint-add"
            disabled={busy || !host.trim() || !portValid}
          >
            <Plus size={14} /> Add
          </Button>
        </form>
      )}
    </div>
  );
}
