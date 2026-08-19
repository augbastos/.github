# Background maintenance

Scheduled secondary maintenance for allowlisted repositories, run by GitHub
Actions and executed by [Jules](https://jules.google). It does not need any
machine of mine to be switched on.

Public repositories are read with the workflow's own token. Private ones - the
Lucky Cat product family - need `MAINTENANCE_READ_TOKEN` and run on a narrower
scope profile; without that secret they are dropped by name in the run summary
rather than silently triaged as empty.

```
schedule (twice daily, 06:17 and 18:17 UTC)
   -> add any repo carrying the 'luckycat-product' topic that is not listed yet
   -> no MAINTENANCE_READ_TOKEN? drop the private repos, by name, in the summary
   -> ask Jules which pull requests it opened (authoritative PR ownership)
   -> review backlog already at MAX_OPEN_MAINTENANCE_PRS?         -> SKIP THE RUN
   -> take the MAX_REPOS_PER_RUN least-recently-maintained repos, each:
      -> already busy? (open maintenance PR or branch, or a live session) -> SKIP
      -> find one justified task, in order: failing CI, then open issues
         (an issue only counts from OWNER / MEMBER / COLLABORATOR),
         then an unchecked MAINTENANCE.md line, then an action ref to pin
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

Priority order, highest first. The first two are **reactive** - something already
broke, or somebody already asked. The last two are **proactive**, and exist
because a healthy repository emits neither of the first two: the loop's early
runs ended in "no failing CI and no actionable open issue" on nearly every
repository, because the CI was green and the owner does not open issues against
his own repositories. A loop that only reacts goes silent exactly when everything
is fine, which is most of the time.

1. **failing CI** on the default branch (a regression by definition, not opinion)
2. **an open issue**, oldest first, from OWNER / MEMBER / COLLABORATOR only
3. **an unchecked line in the repository's own `MAINTENANCE.md`** (or
   `.github/MAINTENANCE.md`) - see below
4. **an action reference that is unpinned or stale** - `uses: foo/bar@v4` pinned
   to the sha it already resolves to, or a sha pin moved up to the publisher's
   latest release

Sources that cannot be verified from the API are not guessed at. There is no
"improve this repository" path and no new-feature path: a feature needs an issue
or a backlog line, which means it arrives as case 2 or 3 or not at all.

### The MAINTENANCE.md backlog

Put a checklist in `.github/MAINTENANCE.md` (or `MAINTENANCE.md`) on the default
branch of any maintained repository:

```markdown
# Maintenance

- [ ] De-flake the timezone test in tests/test_clock.py
- [x] Already done, never picked again
```

The loop takes the **first unchecked line**, one per run, and asks Jules to do
exactly that and tick the box in the same pull request.

Why a file and not an issue: the trust rule for issues is right (only write-level
authors may become a brief) but empty in practice here, because Augusto does not
file issues against himself. A file on the default branch carries the same trust
and more - changing it takes a commit, which takes write access, and the change
is visible in history. A pull request's copy and a fork's copy are never read, so
an outside contributor cannot add a task and have it executed before a merge.

Lines longer than 300 characters are ignored: that is an essay, not a scoped
task.

### Proactive work is offered once

A failing build stops failing and a closed issue stops being open, so the world
forgets those on its own. A backlog line and an action pin do not - they sit
there unchanged until a pull request lands. So each is fingerprinted on
delegation and recorded in `state.json` under `done_work`, and never proposed a
second time.

The cost is that declining a proposal retires it silently. To offer it again,
**edit the wording** of the backlog line (any change of words is new work;
reflowing whitespace is not) or delete its entry from `done_work`.

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

A repository carrying the `luckycat-product` GitHub topic is appended to the allowlist
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

- GitHub Actions: this repository is public, so the minutes are free. Two runs a
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

That cap is not theoretical. On 2026-08-13 the first scheduled run in this
system's life delegated nothing at all: seven Jules pull requests were open on
wavr against a cap of five, so the run stopped before triaging anything. Raising
the cadence at that moment would have changed nothing - a stopped run stops just
as fast twice a day. The cap was raised 5 -> 8 in the same change, deliberately:
a cap that a single repository's batch can exceed on its own has stopped being a
brake on the queue and become a stop on the loop. It is still the first number to
lower if the queue ever outruns the reading.

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
