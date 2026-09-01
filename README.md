# .github

Account-level defaults and shared workflows.

| Path | What it is |
|---|---|
| `.github/FUNDING.yml` | Sponsor button, inherited by every public repository without its own |
| `.github/pull_request_template.md` | Default pull-request template, inherited the same way |
| `.github/workflows/scpe-verify.yml` | [SCPE](https://github.com/augbastos/scpe) check, untrusted half — runs contributor code, holds no secrets |
| `.github/workflows/scpe-seal-reusable.yml` | SCPE check, trusted half — holds the write token, posts the result |
| `.github/workflows/uptime.yml` | Uptime checks for the public endpoints |

This repository is public because a public repository only inherits community health
files from a public `.github`.

## Wiring the SCPE check into a repository

It is two workflows, not one: an untrusted job that runs contributor code with no
secrets, and a trusted job that holds the write token. They cannot be merged, because
a workflow naming itself in `workflow_run.workflows` fails to register.

Consuming repositories keep two thin callers. Copy them from
[`CALLERS.md`](CALLERS.md).
