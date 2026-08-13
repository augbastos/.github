# Background maintenance

Scheduled secondary maintenance for allowlisted repositories, run by GitHub
Actions and executed by [Jules](https://jules.google). It does not need any
machine of mine to be switched on.

Public repositories are read with the workflow's own token. Private ones - the
Lucky Cat product family - need `MAINTENANCE_READ_TOKEN` and run on a narrower
scope profile; without that secret they are dropped by name in the run summary
rather than silently triaged as empty.

```
schedule (daily, 06:17 UTC)
   -> add any repo carrying the 'lucky-cat' topic that is not listed yet
   -> no MAINTENANCE_READ_TOKEN? drop the private repos, by name, in the summary
   -> ask Jules which pull requests it opened (authoritative PR ownership)
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
| `maintenance/repos.json` | the allowlist: visibility, scope profile and frozen paths per repo. Nothing is maintained unless it is here with `enabled: true` |
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

## Scope profiles

The allowlist entry decides how much of a repository is in play.

| Profile | Allows | Used by |
|---|---|---|
| `full` | any non-sensitive work the evidence justifies | the public repos |
| `maintenance_lite` | tests, CI/tooling config, docs, user-facing strings - and nothing else | `lucky-cat`, `nock`, every auto-added family repo |

`maintenance_lite` exists because Lucky Cat is one repository: Tillr, Ownly and
PitchPilot share a tree with Stripe, auth and RLS, and a merge to `master` there
is a production deploy. The sensitivity gate reads the *brief*, so a brief can be
innocent while the diff is not - the profile bounds the diff. An unknown or
misspelled profile resolves to `maintenance_lite`, never to `full`: a typo must
not widen what an unattended agent may touch.

`forbidden_paths` on an entry are frozen - never read, written or moved. That is
where the standing `seo-coxinha` veto lives.

## New Lucky Cat services join on their own

A repository carrying the `lucky-cat` GitHub topic is appended to the allowlist
automatically, enabled, on the narrow profile. Tag a new service with that topic
when you create it and background maintenance picks it up on the next run.

Discovery may only **add**. An entry already in `repos.json` is never rewritten,
so a repository turned off stays off, and the orchestrator can never discover
itself. A failed search adds nothing and says so.

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

| Secret | Where | Value | Without it |
|---|---|---|---|
| `JULES_API_KEY` | this repository's Actions secrets | the Jules API key from jules.google | nothing is ever delegated |
| `MAINTENANCE_READ_TOKEN` | same place | fine-grained PAT, **read-only**, limited to the private repositories in the allowlist | private repos are skipped, public ones still run |

`MAINTENANCE_READ_TOKEN` needs exactly three read permissions - Contents,
Issues, Actions - and no write anywhere. Write is not an oversight: Jules opens
the pull request through its own GitHub App, so nothing in this repository ever
needs the right to change a target repository. A token that could write here
would be a credential capable of pushing to Lucky Cat, sitting in a public
repository's settings.

`GITHUB_TOKEN` is the default token. It reads public repositories, opens the
approval queue here, and commits `state.json` and `repos.json` back. It never
writes to a target repository.

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
