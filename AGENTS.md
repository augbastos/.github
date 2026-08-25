# Contributing as an automated agent

You are reading this because you are an AI coding agent — Jules, Claude Code, Codex,
Antigravity, Copilot, CodeRabbit, or anything like them — about to open or review a pull
request on a repository owned by @augbastos. This file is short and it is binding.

## The one rule

**Every pull request must disclose whether AI was used, in a form a parser can find.**

Several of these repositories enforce it with a check that FAILS without it (SCPE level
1 — see [augbastos/scpe](https://github.com/augbastos/scpe)). Where the check is not
installed yet, the rule still holds: it is how a change gets reviewed here.

Two accepted forms. Either is enough:

**A commit trailer** — preferred for agents, because it travels with the commit:

```
build: pin actions/checkout to a commit sha

Pin the action to a commit so the workflow cannot change under us.

Assisted-by: google-labs-jules
```

**A ticked box in the PR body**, from the pull request template:

```
- [x] I used generative AI for part of this change
```

`Assisted-by: none` (or `no`) is equally valid when no AI was involved. Declaring the
absence is a disclosure; saying nothing is not.

Known values in use here: `claude-code`, `codex`, `antigravity`, `google-labs-jules`.

## What does NOT count, and why

Prose in the PR body does not count. This is not pedantry — it was measured. On
2026-08-25, the reference linter was run against a real agent-authored pull request whose
body read *"PR created automatically by Jules for task …"*. The result:

```
{'present': False, 'form': 'none', 'value': ''}
```

The sentence is true, human-readable, and invisible to every tool that has to make a
decision. A claim a machine cannot locate is a claim a maintainer cannot check at scale.

## Why this exists

A disclosure is not a confession. Agent contributions are welcome here — they are normal
work, and the reason the rule exists is not to discourage them but to keep them
reviewable. What is refused is the question going unanswered.

Whether the answer is TRUE stays a human judgement. A signature (SCPE level 2) makes a
claim attributable and tamper-evident, not true.

## For reviewers

Reviews are not contributions and are not gated. Post them.

If your review results in a commit, that commit follows the rule above.
