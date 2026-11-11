# .github

[![uptime](https://github.com/augbastos/.github/actions/workflows/uptime.yml/badge.svg)](https://github.com/augbastos/.github/actions/workflows/uptime.yml)

Account-level community health defaults for `augbastos` repositories, plus three
GitHub Actions workflows that run on this repository only.

This repository is public because a public repository only inherits community health
files from a public `.github`.

## What it does

- Gives public `augbastos` repositories a default contributing guide, security
  policy, support page, pull-request template, issue forms and sponsor button, unless
  a repository defines its own.
- Runs a safety check on pull requests to this repository.
- Merges Dependabot patch and minor updates on its own once the required checks pass.
- Checks a set of public web addresses every 30 minutes and opens a GitHub issue when
  one is down.

## Layout

| Path | What it is |
|---|---|
| [CONTRIBUTING.md](CONTRIBUTING.md) | Contribution guidelines |
| [SECURITY.md](SECURITY.md) | Vulnerability reporting policy |
| [SUPPORT.md](SUPPORT.md) | Support channels and scope |
| [.github/FUNDING.yml](.github/FUNDING.yml) | Sponsor button, inherited by every public repository without its own |
| [.github/pull_request_template.md](.github/pull_request_template.md) | Default pull-request template, inherited the same way |
| [.github/ISSUE_TEMPLATE/](.github/ISSUE_TEMPLATE/) | Bug report and feature request forms, with contact links |
| [.github/workflows/safety.yml](.github/workflows/safety.yml) | Pull-request safety check |
| [.github/workflows/dependabot-automerge.yml](.github/workflows/dependabot-automerge.yml) | Auto-merge for Dependabot updates |
| [.github/workflows/uptime.yml](.github/workflows/uptime.yml) | Scheduled endpoint checks, runs [scripts/uptime_check.py](scripts/uptime_check.py) |
| [.gitignore](.gitignore) | Keeps local maintenance files out of this public repository |

## Workflows

All three run on GitHub-hosted runners and only on this repository.

- **safety** runs on every pull request. It fails if a commit in the PR introduces a
  secret (scanned with gitleaks), or if a changed file matches a rule in
  `.github/safety-critical.txt` on the base branch. Changes to the check itself also
  need the owner.
- **dependabot-automerge** arms squash auto-merge on Dependabot pull requests that are
  not major version updates. Major updates stay open for review.
- **uptime** runs every 30 minutes and on manual dispatch. For each address,
  `scripts/uptime_check.py` retries 3 times before it reports a failure, then opens one
  issue labelled `uptime` per outage, updates it while the outage lasts, and closes it
  when the address answers again. GitHub scheduling is not exact, so runs can be late.

## Licence

This repository has no licence file.
