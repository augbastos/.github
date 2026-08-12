# Reusable workflows — what a consuming repository needs

The SCPE check is two workflows, not one: an **untrusted** job that runs contributor
code with no secrets, and a **trusted** job that holds the write token and posts the
result. They cannot be merged — a workflow that names itself in
`workflow_run.workflows` fails to register with GitHub.

Both bodies live here, once:

| File | Half | Trigger it serves |
|---|---|---|
| `.github/workflows/scpe-verify.yml` | untrusted | the caller's `pull_request` |
| `.github/workflows/scpe-seal-reusable.yml` | trusted | the caller's `workflow_run` |

A consuming repository keeps two thin callers. Copy them verbatim.

### `.github/workflows/scpe.yml`

```yaml
name: scpe

on:
  pull_request:

permissions:
  contents: read

jobs:
  verify:
    uses: augbastos/.github/.github/workflows/scpe-verify.yml@main
    with:
      level: "1"                   # 1 = disclosure lint · 2 = signed envelope required
      require: "false"             # "true" makes a missing disclosure fail the check
```

### `.github/workflows/scpe-seal.yml`

```yaml
name: scpe-seal

on:
  workflow_run:
    workflows: ["scpe"]
    types: [completed]

permissions:
  contents: read

jobs:
  seal:
    permissions:
      pull-requests: write
      actions: read
      contents: read
    uses: augbastos/.github/.github/workflows/scpe-seal-reusable.yml@main
```

## The four things that break this

1. **The `name:` of the first caller must match `workflow_run.workflows`.** They are
   matched by workflow *name*, not by filename. Rename `scpe` and the seal never fires.
2. **The trigger cannot move into the reusable workflow.** `workflow_run` matches a
   workflow in the *calling* repository, so that trigger has to stay in the caller.
3. **Permissions are granted by the caller.** A called workflow can narrow the token it
   is handed, never widen it. Drop `pull-requests: write` or `actions: read` from the
   `seal` job and the download or the comment fails.
4. **`workflow_run` only fires for a file on the caller's DEFAULT branch.** Both callers
   have to be merged to `master`/`main` before any pull request gets a seal.

## Versioning

The callers track `@main` deliberately: the point of the split is that one edit here
reaches every repository. The pinned Action tag (`augbastos/scpe@vX.Y.Z`) lives inside
`scpe-verify.yml` because `uses:` does not accept `${{ }}` expressions — the version
cannot be an input, so bumping every consumer is a one-line edit in this repository.

## Who calls it today

- `augbastos/wavr`
- `augbastos/scrcpy-tray`
