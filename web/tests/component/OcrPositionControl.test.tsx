// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import type { ComponentProps } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { EngineOption } from '../../src/api/types';
import { OcrParamsBody } from '../../src/components/action-panel/MediaReadParamsBodies';

type Props = ComponentProps<typeof OcrParamsBody>;

const genericVision: EngineOption = {
  id: 'example/vision-model',
  label: 'Example vision model',
  tier: 'hosted',
  billable: true,
  available: true,
  ocr_options: { keep_text_position: true },
};

const dedicated: EngineOption = {
  id: 'rapidocr',
  label: 'RapidOCR',
  tier: 'local',
  available: true,
};

function renderBody(engine: EngineOption, keepTextPosition?: boolean) {
  const setParams = vi.fn<Props['setParams']>();
  const params: Props['params'] = {
    source: 'scan',
    engine: engine.id,
    ...(keepTextPosition === undefined ? {} : { keep_text_position: keepTextPosition }),
  };
  const Field: Props['Field'] = ({ name, label }) => (
    <div data-testid={`field-${name}`}>{label ?? name}</div>
  );
  render(<OcrParamsBody
    params={params}
    setParams={setParams}
    errors={{}}
    engine={engine}
    Field={Field}
    sheet={null}
    request={{ scope: { kind: 'sheet_rows', sheet_id: 1 }, output_names: {} }}
  />);
  return { params, setParams };
}

afterEach(() => cleanup());

describe('generic vision OCR position control', () => {
  it('defaults on and authors an explicit opt-out for a declared generic VLM', () => {
    const { params, setParams } = renderBody(genericVision);

    const checkbox = screen.getByTestId('ocr-keep-text-position');
    expect(checkbox).toBeChecked();
    expect(checkbox.closest('label')).toHaveTextContent('Keep text position');

    fireEvent.click(checkbox);
    expect(setParams).toHaveBeenCalledWith({ ...params, keep_text_position: false });
  });

  it('preserves a saved opt-out and stays absent for dedicated OCR engines', () => {
    renderBody(genericVision, false);
    expect(screen.getByTestId('ocr-keep-text-position')).not.toBeChecked();

    cleanup();
    renderBody(dedicated);
    expect(screen.queryByTestId('ocr-keep-text-position')).not.toBeInTheDocument();
  });
});
