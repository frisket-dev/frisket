import { describe, expect, it } from 'vitest';
import cases from '../../../tests/fixtures/evaluation-match-cases.json';
import { matchAnswer } from '../../src/workbench/evaluationMatching';

describe('shared evaluation matching cases', () => {
  for (const testCase of cases) {
    it(testCase.note, () => {
      expect(matchAnswer(testCase.rule, testCase.expected, testCase.answer, {
        state: testCase.state, handVerdict: testCase.handVerdict,
      })).toBe(testCase.verdict);
    });
  }
});
