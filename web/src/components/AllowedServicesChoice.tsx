import './AllowedServicesChoice.css';
import { useId } from 'react';
import type { AllowedServices } from '../api/types';
import {
  ALLOWED_SERVICES_DESCRIPTIONS,
  ALLOWED_SERVICES_LABELS,
  ALLOWED_SERVICES_ORDER,
} from '../allowedServices';
import { Button } from './Button';

/** The Allowed services radio cards, shared by the New project dialog and
 *  project settings. Only the values the edition offers are shown. */
export function AllowedServicesChoice({
  value,
  offered,
  onChange,
  disabled = false,
  onManageEndpoints,
  legendVisible = true,
}: {
  value: AllowedServices;
  offered: readonly AllowedServices[];
  onChange(next: AllowedServices): void;
  disabled?: boolean;
  onManageEndpoints?: () => void;
  /** False where a heading already names the setting; the group keeps its label. */
  legendVisible?: boolean;
}) {
  const name = useId();
  const legendId = useId();
  const shown = ALLOWED_SERVICES_ORDER.filter((option) => offered.includes(option) || option === value);
  return (
    <fieldset
      className="allowed-services"
      role="radiogroup"
      aria-labelledby={legendId}
      disabled={disabled}
      data-testid="allowed-services"
    >
      <legend className={legendVisible ? 'allowed-services-legend' : 'sr-only'} id={legendId}>Allowed services</legend>
      <div className="allowed-services-options">
        {shown.map((option) => (
          <label
            key={option}
            className="allowed-services-option"
            data-selected={option === value}
            data-testid={`allowed-services-${option}`}
          >
            <input
              type="radio"
              name={name}
              value={option}
              checked={option === value}
              onChange={() => onChange(option)}
            />
            <span className="allowed-services-text">
              <span className="allowed-services-title">{ALLOWED_SERVICES_LABELS[option]}</span>
              <span className="allowed-services-description">{ALLOWED_SERVICES_DESCRIPTIONS[option]}</span>
            </span>
          </label>
        ))}
      </div>
      {value !== 'all_services' && (
        <div className="allowed-services-note">
          <span className="allowed-services-disclaimer" data-testid="allowed-services-disclaimer">
            Best-effort. Not network isolation.
          </span>
          {value === 'approved_services' && onManageEndpoints && (
            <Button
              type="button"
              variant="link"
              className="allowed-services-manage"
              data-testid="manage-approved-endpoints"
              onClick={onManageEndpoints}
            >
              Manage approved endpoints
            </Button>
          )}
        </div>
      )}
    </fieldset>
  );
}
