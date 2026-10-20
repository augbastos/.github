# .github

Account-level community health defaults and shared workflows for `augbastos` repositories.

| Path | What it is |
|---|---|
| [CONTRIBUTING.md](CONTRIBUTING.md) | Contribution guidelines |
| [SECURITY.md](SECURITY.md) | Vulnerability reporting policy |
| [SUPPORT.md](SUPPORT.md) | Support channels and scope |
| [.github/FUNDING.yml](.github/FUNDING.yml) | Sponsor button, inherited by every public repository without its own |
| [.github/pull_request_template.md](.github/pull_request_template.md) | Default pull-request template, inherited the same way |
| [.github/ISSUE_TEMPLATE/](.github/ISSUE_TEMPLATE/) | Bug report and feature request forms, with contact links |
| [.github/workflows/scpe-verify.yml](.github/workflows/scpe-verify.yml) | Reusable [SCPE](https://github.com/augbastos/scpe) check with read-only repository permissions |
| [.github/workflows/scpe-seal-reusable.yml](.github/workflows/scpe-seal-reusable.yml) | Reusable workflow that posts the SCPE result to the pull request |
| [.github/workflows/uptime.yml](.github/workflows/uptime.yml) | Scheduled public endpoint checks using [scripts/uptime_check.py](scripts/uptime_check.py) |

This repository is public because a public repository only inherits community health
files from a public `.github`.

## Wiring the SCPE check into a repository

It is two workflows, not one: an untrusted job that runs contributor code with no
secrets, and a trusted job that holds the write token. They cannot be merged, because
a workflow naming itself in `workflow_run.workflows` fails to register.

Reusable workflows require explicit callers in each consuming repository. Copy the two callers from
[`CALLERS.md`](CALLERS.md).
