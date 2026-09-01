# .github

The account-level `.github` repository. Several unrelated things live here because
GitHub requires each of them to live here.

## 1. Community health defaults

`.github/FUNDING.yml` and `.github/pull_request_template.md` are inherited by every
public repository on the account that does not define its own. This repository is
public for that reason alone: a public repository will not inherit community health
files from a private `.github`.

[`AGENTS.md`](AGENTS.md) is the binding rule for automated agents opening pull
requests here — every pull request must disclose whether AI was used, in a form a
parser can find. Several repositories fail the check without it.

## 2. Background maintenance

A scheduled loop that finds justified maintenance work in allowlisted repositories
and delegates one task at a time to [Jules](https://jules.google), running on GitHub
Actions with no machine of mine switched on. **Nothing is ever merged by it** — every
result arrives as a pull request awaiting review.

Being public is not consent: a repository is maintained only if it is listed in
`maintenance/repos.json` with `enabled: true`. Sensitive areas are gated by
`maintenance/sensitive_task.py`, and finding nothing is a successful run.

Full description, including the triage order and the scope profiles, in
[`maintenance/README.md`](maintenance/README.md).

| Path | What it is |
|---|---|
| `.github/workflows/jules-maintenance.yml` | the schedule and the only entry point |
| `maintenance/repos.json` | the allowlist: visibility, scope profile, frozen paths |
| `maintenance/triage.py` | rotation, triage, duplicate check, brief, Jules call |
| `maintenance/sensitive_task.py` | what may never be delegated unattended |

## 3. Reusable SCPE workflows

[SCPE](https://github.com/augbastos/scpe) verification, as two reusable workflows
that consuming repositories call with thin wrappers.

It has to be two: an **untrusted** job that runs contributor code holding no secrets,
and a **trusted** job that holds the write token and posts the result. They cannot be
merged into one, because a workflow naming itself in `workflow_run.workflows` fails
to register with GitHub.

| File | Half | Caller trigger |
|---|---|---|
| `.github/workflows/scpe-verify.yml` | untrusted | `pull_request` |
| `.github/workflows/scpe-seal-reusable.yml` | trusted | `workflow_run` |

The callers to copy verbatim are in [`CALLERS.md`](CALLERS.md).

## 4. Uptime checks

`.github/workflows/uptime.yml` watches the public addresses that had nothing watching
them: the portfolio, devcard, Tillr, Ownly, the SCPE page and the Lucky Cat site.
`luckycat.ie` is monitored separately and more thoroughly elsewhere, because it is
the one with a payment path on it.

It runs free and without a deadline. GitHub Actions is free for public repositories
on standard runners, which is exactly what this is — there is no allowance here to
overrun.
