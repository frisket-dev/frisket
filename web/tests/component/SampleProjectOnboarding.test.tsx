// @vitest-environment jsdom
import { StrictMode } from 'react';
import { cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { GuidanceReadyContext } from '../../src/workbench/guidanceReady';
import { SampleProjectOnboarding } from '../../src/workbench/SampleProjectOnboarding';
import { setSampleGuideArrival } from '../../src/workbench/sampleGuideArrival';

const enter = vi.hoisted(() => vi.fn());
vi.mock('../../src/walkthrough/context', () => ({ useWalkthrough: () => ({ enterSampleProject: enter }) }));
afterEach(() => { cleanup(); enter.mockClear(); setSampleGuideArrival(null); });

function Sample({ ready = true, name = 'Sample project', id = 'demo' }) {
  return <GuidanceReadyContext.Provider value={ready}>
    <SampleProjectOnboarding project={{ id, name }} />
  </GuidanceReadyContext.Provider>;
}

describe('sample arrival', () => {
  it('holds a guided arrival until disclosures finish and enters once under StrictMode', () => {
    setSampleGuideArrival('demo');
    const view = render(<StrictMode><Sample ready={false} /></StrictMode>);
    expect(enter).not.toHaveBeenCalled();
    view.rerender(<StrictMode><Sample /></StrictMode>);
    expect(enter).toHaveBeenCalledExactlyOnceWith({ openGuide: true });
    view.rerender(<StrictMode><Sample /></StrictMode>);
    expect(enter).toHaveBeenCalledTimes(1);
  });

  it('recognizes normal sample opens by the existing name only', () => {
    render(<Sample id="another-id" />);
    expect(enter).toHaveBeenCalledExactlyOnceWith({ openGuide: false });
  });

  it('never prompts for ordinary projects and drops stale guided intent', () => {
    setSampleGuideArrival('demo');
    const view = render(<Sample name="My reporting" id="regular" />);
    expect(enter).not.toHaveBeenCalled();
    view.rerender(<Sample />);
    expect(enter).toHaveBeenCalledExactlyOnceWith({ openGuide: false });
  });
});
