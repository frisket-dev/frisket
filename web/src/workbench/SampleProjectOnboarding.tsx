import { useEffect, useRef } from 'react';
import type { ProjectInfo } from '../api/open';
import { useWalkthrough } from '../walkthrough/context';
import { useGuidanceReady } from './guidanceReady';
import { consumeSampleGuideArrival } from './sampleGuideArrival';

/** Run after the destination workspace and existing first-use dialogs are ready. */
export function SampleProjectOnboarding({ project }: { project: Pick<ProjectInfo, 'id' | 'name'> }) {
  const ready = useGuidanceReady();
  const { enterSampleProject } = useWalkthrough();
  const enteredProject = useRef<string | null>(null);

  useEffect(() => {
    if (!ready || enteredProject.current === project.id) return;
    enteredProject.current = project.id;
    const openGuide = consumeSampleGuideArrival(project.id);
    if (project.name === 'Sample project') enterSampleProject({ openGuide });
  }, [ready, project.id, project.name, enterSampleProject]);

  return null;
}
