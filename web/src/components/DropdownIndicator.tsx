import { ChevronDown } from 'lucide-react';
import './DropdownIndicator.css';

/** Shared visual affordance only; the parent owns menu/select/dialog behavior. */
export function DropdownIndicator({ className = '' }: { className?: string }) {
  return <ChevronDown className={`dropdown-indicator ${className}`} aria-hidden="true" focusable="false" />;
}
