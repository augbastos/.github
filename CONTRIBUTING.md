# Contributing

This file is the account-level default. GitHub serves it for any public repository under
`augbastos` that does not ship its own, so it has to be true everywhere. Where a repository
needs different rules, it overrides this file rather than arguing with it.

These projects are maintained by one person. That shapes everything below: there is no team to
review your PR, no rota, and no service-level promise. What there is, is a short and honest
description of what happens to a contribution.

## Before you open a pull request

- **Open an issue first for anything non-trivial.** A rejected 400-line PR wastes your evening,
  not mine. A three-line issue costs you nothing and gets you an answer.
- Typo fixes, broken links and obviously-correct one-liners need no issue. Just send them.

## Branches

```
<type>/<short-kebab-description>
```

`feat` `fix` `hotfix` `refactor` `perf` `test` `docs` `ci` `build` `chore` `security` `audit`
`release`

`feat/csv-export`, `fix/timezone-off-by-one`, `docs/install-on-windows`.

Not `patch-1`, not `my-changes`, not your name, not the name of the tool that wrote the code.
A branch describes the work.

## Commits

[Conventional Commits](https://www.conventionalcommits.org/):

```
fix(parser): reject a trailing comma instead of silently dropping the field
```

The subject is the imperative. The body, when there is one, says *why* - the diff already says
what.

For squash-merged PRs, the **PR title** becomes the commit subject, so the PR title is the one
that has to follow this. Individual commits on your branch can be as messy as your work was.

## Pull requests

- one logical change per PR
- tests for behaviour you changed, if the repository has tests
- CI green, and green because it passes, not because a check was removed
- fill in the AI-use box in the template

That last one is not a formality. Several of these repositories gate on
[SCPE](https://github.com/augbastos/scpe) and a PR with no disclosure fails the check. Both
answers are fine; only silence fails. Naming the tool and what you used it for helps a reviewer
calibrate, and that is the whole point of asking.

## What happens next

I read PRs when I can, which is evenings and weekends in Ireland. If a week goes past with no
response, a comment on the PR is welcome and not rude.

I will tell you plainly if I am not going to merge something, and why. A PR left open and
silent forever is a worse outcome than a no.

## Licence

By contributing you agree your contribution is licensed under the repository's licence. Check
`LICENSE` before you start: these repositories are not all under the same terms, and `wavr` in
particular is AGPL-3.0, which has consequences you may care about.

## Security

Do not open an issue for a vulnerability. See [SECURITY.md](SECURITY.md).
