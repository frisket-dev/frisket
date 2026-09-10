import { CheckCircle2 } from 'lucide-react';
import type { ImportWorkspaceStage, ImportWorkspaceStageOption } from './model';

interface ImportStageStepperProps {
  stages: ImportWorkspaceStageOption[];
  stage: ImportWorkspaceStage;
}

export function ImportStageStepper({ stages, stage }: ImportStageStepperProps) {
  const currentStageIndex = stages.findIndex((candidate) => candidate.id === stage);
  return (
    <div className="import-workspace-stages" aria-label="Import stages">
      {stages.map((stageItem, index) => (
        <div
          key={stageItem.id}
          className={`import-workspace-stage${stageItem.id === stage ? ' import-workspace-stage-active' : ''}`}
          data-testid={stageItem.testId}
          aria-current={stageItem.id === stage ? 'step' : undefined}
        >
          {currentStageIndex > index ? <CheckCircle2 size={14} /> : <span>{index + 1}</span>}
          {stageItem.label}
        </div>
      ))}
    </div>
  );
}
