# Security Policy

frisket is a local-first tool that runs on your own machine (or a
self-hosted team server) and can execute model-backed and sandboxed ops
against your data. We take reports of security issues in the open product
seriously and appreciate responsible disclosure.

## Reporting a vulnerability

**Preferred: GitHub private vulnerability reporting.** This repository has
[private vulnerability reporting][pvr] enabled via GitHub Security
Advisories. Open a draft advisory from the **Security** tab
(`Security` → `Advisories` → `Report a vulnerability`) instead of filing a
public issue. This goes directly to maintainers, needs no email address, and
lets us collaborate with you on a fix before anything is disclosed publicly.

[pvr]: https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing/privately-reporting-a-security-vulnerability

<!-- TODO(security-contact): confirm reporting address -->
If you cannot use GitHub's advisory flow, a fallback email address will be
published here — until then, private advisories are the only supported
channel.

Please do not report security vulnerabilities through public GitHub issues,
discussions, or pull requests.

### What to include

- A description of the vulnerability and its potential impact.
- Steps to reproduce, or a minimal proof of concept.
- The affected version/commit and your environment (OS, install method —
  pip/uvx, docker/self-host, source checkout).
- Any suggested remediation, if you have one.

## Supported versions

frisket is pre-1.0 (currently `0.1.0`) and not yet published to PyPI —
see `README.md`. Until a 1.0 release and a versioning policy exist, only the
latest commit on `main` (and the most recent tagged/built release artifact,
where one exists) is supported. There are no LTS or backport branches at
this stage; fixes land on `main` and are picked up by the next release.

| Version | Supported |
| --- | --- |
| `main` (latest) | Yes |
| Anything older | No |

## Responsible disclosure expectations

- Give us a reasonable window to investigate and ship a fix before any
  public disclosure — we'll work with you on timing, and will keep you
  updated as we triage.
- Make a good-faith effort to avoid privacy violations, data destruction,
  and service disruption while investigating. Only interact with your own
  data/instances; do not attempt to access another user's data.
- Scope: the `frisket` package (local single-user product and
  single-organization team server) and this repository's source, workflows,
  and released artifacts.
- We do not currently run a paid bug bounty program.

Thank you for helping keep frisket and its users safe.
