# Background maintenance

Scheduled secondary maintenance for allowlisted public repositories, run by
GitHub Actions and executed by [Jules](https://jules.google). It does not need
any machine of mine to be switched on.

```
schedule (daily, 06:17 UTC)
   -> review backlog already at MAX_OPEN_MAINTENANCE_PRS?         -> SKIP THE RUN
   -> take the MAX_REPOS_PER_RUN least-recently-maintained repos, each:
      -> already busy? (open maintenance PR or branch, or a live session) -> SKIP
      -> find one justified task: failing CI first, then open issues
         (an issue only counts from OWNER / MEMBER / COLLABORATOR)
         -> nothing found                                          -> SKIP
         -> touches a sensitive area -> open an approval issue here -> HELD
         -> otherwise: create ONE Jules session (AUTO_CREATE_PR)
            -> Jules opens a branch + pull request
               -> nothing is merged, ever, by this system
```

Finding nothing is a successful run. The system maintains repositories; it does
not manufacture activity.

## Files

| Path | What it is |
|---|---|
| `.github/workflows/jules-maintenance.yml` | the schedule, the permissions, the only entry point |
| `maintenance/repos.json` | the allowlist. Nothing is maintained unless it is here with `enabled: true` |
| `maintenance/triage.py` | repo rotation, triage, duplicate check, brief builder, Jules call |
| `maintenance/sensitive_task.py` | the gate that decides what may never be delegated unattended |
| `maintenance/tests/test_gate.py` | adversarial tests for that gate; the workflow runs them before it trusts it |
| `maintenance/state.json` | rotation state and run history, committed back by the workflow |
| `maintenance/last-run.json` | the most recent run record (also uploaded as an artifact) |

## Choosing what to work on

Priority order, highest first:

1. failing CI on the default branch (a regression by definition, not by opinion)
2. an open issue, oldest first

Sources that cannot be verified from the API are not guessed at. There is no
"improve this repository" path, and no new-feature path: a feature needs an
issue or an explicit TODO, which means it arrives as case 2 or not at all.

## What is never delegated

`sensitive_task.py` refuses anything touching authentication, payments,
billing, secrets and credentials, deploys, releases, publishing, production
systems, destructive database migrations, row-level security, or destructive
repository operations. A hit is recorded for human review, not delegated.

The rule for "production" is contextual: `deploy to production` and
`production database` are refused, `do not change production code` is not. The
comments in that file explain why, and `tests/test_gate.py` pins both senses.

## Rotation

Least-recently-maintained first, `priority` breaking ties. Skipped runs still
stamp `last_run`, so a repository that is permanently busy or permanently clean
cannot pin every future run to itself. Not random: with a handful of
repositories, randomness produces exactly the starvation this avoids.

## Cost and quota

- GitHub Actions: this repository is public, so the minutes are free. One run a
  day, each a few minutes.
- Jules: at most `MAX_REPOS_PER_RUN` tasks per run, and never more than one per
  repository. The Google AI Pro allowance is 100 tasks per rolling 24 hours with
  15 concurrent, so even a maximal day is a few percent of it.
- A skipped run costs no Jules quota at all.

**Quota is not what bounds this.** Review capacity is. The real cap is
`MAX_OPEN_MAINTENANCE_PRS`: once that many maintenance pull requests are open
across the allowlist, a run creates nothing at all until the queue is cleared.
A backlog nobody can read gets rubber-stamped, and a rubber-stamped agent patch
reaching master is the failure this whole system is arranged to avoid. Raise the
cadence freely; raise that cap only if the reviewing actually happens.

## Secrets

One, and only one:

| Secret | Where | Value |
|---|---|---|
| `JULES_API_KEY` | this repository's Actions secrets | the Jules API key from jules.google |

`GITHUB_TOKEN` is the default token. It is used only to read public repositories
and to commit `state.json` back here. It never writes to a target repository -
Jules pushes through its own GitHub App installation.

## Reviewing the results

From Claude Code:

```powershell
pwsh -NoProfile -File ~/.claude/scripts/maintenance-status.ps1
```

It lists open `jules-maintenance` pull requests across the allowlist, the last
run record, and the live Jules sessions. Review, then merge or close by hand.

## Turning it off

| Goal | Action |
|---|---|
| pause one repository | set `enabled: false` in `maintenance/repos.json` |
| pause everything, keep the code | comment out the `schedule:` block in the workflow |
| stop it completely | delete `.github/workflows/jules-maintenance.yml` |
| remove every trace | delete that workflow and the `maintenance/` directory, then delete the `JULES_API_KEY` secret |

Nothing outside this repository is modified by installing or removing it. The
target repositories carry no workflow, no secret and no configuration from this
system - which is the reason it is built centrally.
