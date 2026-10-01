// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AskResearchSettings, type AskResearchOptions } from '../../src/components/project-ask/AskResearchSettings';

afterEach(cleanup);

const options: AskResearchOptions = {
  write_mode: 'ask_overwrite',
  budget_usd: null,
  max_turns: null,
  skills: null,
};

describe('AskResearchSettings', () => {
  it('enables research with safe defaults without inventing a budget', async () => {
    const onChange = vi.fn();
    render(<AskResearchSettings value={undefined} onChange={onChange} availableSkills={[]} />);

    await userEvent.click(screen.getByRole('checkbox', { name: 'Run actions' }));

    expect(onChange).toHaveBeenCalledWith(options);
  });

  it('edits write access, total budget, and max turns through existing controls', () => {
    const onChange = vi.fn();
    const { rerender } = render(<AskResearchSettings value={options} onChange={onChange} availableSkills={[]} />);

    expect(screen.getByText('Uses your current action approval limit when blank.')).toBeVisible();
    fireEvent.change(screen.getByLabelText('Write access'), { target: { value: 'ask_each' } });
    expect(onChange).toHaveBeenLastCalledWith({ ...options, write_mode: 'ask_each' });

    fireEvent.change(screen.getByLabelText('Total budget (USD)'), { target: { value: '12.500001' } });
    expect(onChange).toHaveBeenLastCalledWith({ ...options, budget_usd: '12.500001' });

    fireEvent.change(screen.getByLabelText('Max turns'), { target: { value: '7' } });
    expect(onChange).toHaveBeenLastCalledWith({ ...options, max_turns: 7 });
    rerender(<AskResearchSettings value={{ ...options, max_turns: 7 }} onChange={onChange} availableSkills={[]} />);
    fireEvent.change(screen.getByLabelText('Max turns'), { target: { value: '' } });
    expect(onChange).toHaveBeenLastCalledWith({ ...options, max_turns: null });
  });

  it('uses canonical skill names and distinguishes all skills from no skills', async () => {
    const onChange = vi.fn();
    const { rerender } = render(<AskResearchSettings
      value={options}
      onChange={onChange}
      effectiveWebProvider="Tavily"
      availableSkills={[
        { name: 'documents', description: 'Research documents' },
        { name: 'tables', description: 'Analyze tables' },
      ]}
    />);

    expect(screen.getByText('Web searches use Tavily.')).toBeVisible();
    expect(screen.getByText('Research documents')).toBeVisible();
    expect(screen.getByRole('checkbox', { name: 'documents' })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: 'tables' })).toBeChecked();

    await userEvent.click(screen.getByRole('checkbox', { name: 'documents' }));
    expect(onChange).toHaveBeenLastCalledWith({ ...options, skills: ['tables'] });

    rerender(<AskResearchSettings value={{ ...options, skills: ['tables'] }} onChange={onChange} availableSkills={[
      { name: 'documents', description: 'Research documents' },
      { name: 'tables', description: 'Analyze tables' },
    ]} />);
    await userEvent.click(screen.getByRole('checkbox', { name: 'documents' }));
    expect(onChange).toHaveBeenLastCalledWith({ ...options, skills: null });

    rerender(<AskResearchSettings value={{ ...options, skills: [] }} onChange={onChange} availableSkills={[
      { name: 'documents', description: 'Research documents' },
      { name: 'tables', description: 'Analyze tables' },
    ]} />);
    expect(screen.getByRole('checkbox', { name: 'documents' })).not.toBeChecked();
    expect(screen.getByRole('checkbox', { name: 'tables' })).not.toBeChecked();
  });

  it('does not emit malformed budgets or non-positive turn limits', () => {
    const onChange = vi.fn();
    render(<AskResearchSettings value={options} onChange={onChange} availableSkills={[]} />);

    fireEvent.change(screen.getByLabelText('Total budget (USD)'), { target: { value: '1.0000001' } });
    fireEvent.change(screen.getByLabelText('Max turns'), { target: { value: '0' } });

    expect(onChange).not.toHaveBeenCalled();
  });
});
