---
name: review-change
description: Review one Frisket change (a branch or pull request) in a fresh context before a person looks at it, against its task and against CODING_STANDARDS.md, AGENTS.md and GLOSSARY.md. Use after implementation, in a session that did not write the change.
---

# Review a change

You did not write this change. Judge it from the diff, the code around it and
the task, not from the author's description of it.

## Inputs

- Base and head refs, or a PR number (`gh pr diff <n>`, `gh pr view <n>`).
- The task brief or issue the change was meant to satisfy, if one exists.
  Without one, say so and review against the PR's own "What and why".

Read `CODING_STANDARDS.md`, the Conventions in `AGENTS.md`, `GLOSSARY.md` and
`documentation/adr/` before the diff.

## Two axes, reported separately

**Against the task**
- Asked for and missing.
- Built and not asked for.
- Implemented differently than specified.

**Against the standards**
- Breaches of `CODING_STANDARDS.md` and the `AGENTS.md` conventions.
- Names and user-facing text that use a word the glossary rejects, or a
  glossary term in a sense it does not define.
- Behaviour that contradicts a glossary definition or a decision record,
  such as a run replacing a source column.

## Evidence

- Every finding cites `file:line` in the head revision.
- Read the surrounding code and the callers before writing a finding.
- Verify by running the narrowest command that settles it: one test, one
  gate script, one `git grep`. Report the command and its result.
- List what you did not check.

## Acting on findings

- Fix mechanical findings directly on the branch when the fix is small and
  safe: a name, a missing use of an existing helper, a restated literal.
  One commit per fix; rerun the check that covers it.
- Report, without changing, every judgment call and anything on a
  one-way-door path: `src/frisket/upgrades/`, authentication and team
  security, billing and consent in `src/frisket/execution/`, secrets,
  `.github/`, release scripts, `scripts/ci/` baselines and allowlists,
  `GLOSSARY.md`.
- Never loosen a gate, baseline or test to make a finding go away.

## Output

```
Verdict: ready | ready after fixes applied | needs a person: <why>

Against the task
- <file:line> <finding> (<command run, result>)

Against the standards
- <file:line> <rule> <finding> [fixed in <sha>]

Not checked
- <area or claim, and why>
```

Keep each finding to one or two lines. No praise, no summary of the diff.
