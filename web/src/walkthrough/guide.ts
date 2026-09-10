import type { WalkthroughDefinition, WalkthroughInstruction } from './walkthroughs';

export interface WalkthroughMarkdownOptions {
  imageHrefForStep?: (
    step: WalkthroughDefinition['steps'][number],
    stepIndex: number,
  ) => string | undefined;
}

function instructionToMarkdown(instruction: WalkthroughInstruction): string {
  if (typeof instruction === 'string') return instruction;
  const explanation = instruction.explanation ? ` ${instruction.explanation}` : '';
  const learnMore = instruction.learnMore
    ? ` [${instruction.learnMore.label}](${instruction.learnMore.href}).`
    : '';
  return `${instruction.lead} \`${instruction.code}\`.${explanation}${learnMore}`;
}

/** Render the same ordered walkthrough copy as a portable written guide. */
export function walkthroughToMarkdown(
  walkthrough: WalkthroughDefinition,
  options: WalkthroughMarkdownOptions = {},
): string {
  const lines = [
    `# ${walkthrough.title}`,
    '',
    walkthrough.description,
    '',
  ];
  if (walkthrough.requirements) {
    lines.push(`**Requires:** ${walkthrough.requirements}`, '');
  }
  walkthrough.steps.forEach((step, index) => {
    lines.push(
      `## ${index + 1}. ${step.title}`,
      '',
      instructionToMarkdown(step.instruction),
      '',
    );
    if (step.expected) lines.push(`**Then:** ${step.expected}`, '');
    const imageHref = options.imageHrefForStep?.(step, index);
    if (imageHref) lines.push(`![${index + 1}. ${step.title}](${imageHref})`, '');
  });
  return `${lines.join('\n').trim()}\n`;
}
