import { CheckCircle2 } from 'lucide-react';
import './StageStepper.css';
interface StageOption {
  id: string;
  label: string;
  testId: string;
}

interface StageStepperProps {
  stages: StageOption[];
  stage: string;
  label: string;
}

export function StageStepper({ stages, stage, label }: StageStepperProps) {
  const currentStageIndex = stages.findIndex((candidate) => candidate.id === stage);
  return (
    <div className="stage-stepper" aria-label={label}>
      {stages.map((stageItem, index) => (
        <div
          key={stageItem.id}
          className="stage-stepper-item"
          data-state={stageItem.id === stage ? 'active' : currentStageIndex > index ? 'done' : undefined}
          data-testid={stageItem.testId}
          aria-current={stageItem.id === stage ? 'step' : undefined}
        >
          {currentStageIndex > index ? <CheckCircle2 size={14} aria-hidden /> : <span>{index + 1}</span>}
          {stageItem.label}
        </div>
      ))}
    </div>
  );
}
