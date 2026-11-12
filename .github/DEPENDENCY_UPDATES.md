# Dependency updates

Dependabot opens **security fixes only**, grouped by ecosystem/runtime. Routine
version updates are disabled. Major upgrades, model runtimes, Desktop runtime
packages, sidecar environments, experiments, and workflow changes need manual
review. Ordinary application patch/minor updates in the `npm-security` and
`python-security` groups can auto-merge after required
checks pass. Merging never publishes a release.

## One-time repository setup

In **Settings → Branches → main protection** (or the rule/ruleset protecting
`main`), retain existing protections and require these GitHub Actions checks:

- `deterministic-source`
- `ci-pass`
- `macos-portability`
- `windows-portability`
- `desktop-macos-ci`
- `desktop-windows-ci`
- `docker-ci`

Require branches to be up to date before merging. The summary checks always
report on PRs, including when their expensive jobs legitimately skip for an
unrelated change. Failures or cancellations cannot satisfy them. Choose the
GitHub Actions source for each check if the settings offer a source selector.

Keep **Dependabot alerts** and **Dependabot security updates** enabled under
Settings → Advanced Security. Grouping is defined in `.github/dependabot.yml`;
there is no need to enable routine version updates or dismiss unresolved alerts.

After protection is saved, set the **repository Actions variable**
`DEPENDABOT_AUTOMERGE_ENABLED` to `true` under Settings → Secrets and variables →
Actions → Variables. Leave it unset to prevent enabling auto-merge. To stop an already queued merge,
use Disable auto-merge on that PR. Repository
**Allow auto-merge** must also be enabled. If the branch requires approvals,
those approvals remain required; the workflow does not approve or bypass rules.

The workflow reuses `PRICING_APP_ID` / `PRICING_APP_PRIVATE_KEY`, the existing
repository PR automation App. Its token is restricted to this repository with
Contents and Pull requests write permissions. Using an App ensures merges
trigger normal main CI. It never checks out or executes Dependabot PR code.

## Ongoing handling

New or updated eligible grouped Dependabot PRs get auto-merge enabled. Existing
standalone PRs stay manual until Dependabot replaces them with groups. Each
update clears any previously enabled auto-merge before rechecking eligibility. Failed checks
leave the PR open. Fix or replace genuinely broken updates; close obsolete PRs
only once their fixes are included elsewhere. A major security fix still needs
attention even though it cannot auto-merge. Dependabot may replace individual
PRs with grouped PRs when this policy is enabled.
