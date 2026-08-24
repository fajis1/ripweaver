# Git Checkpoints

RipWeaver uses remote `wip/*` branches as recovery checkpoints for meaningful
work that is not ready to merge. A checkpoint is stored in the owner-controlled
GitHub repository but does not change `main`, create a release, or authorize any
media operation.

## Checkpoint channels

- `wip/test`: current development and test work.
- `wip/main`: emergency checkpoint for work performed directly on `main`.

The normal `origin/main` branch already protects committed mainline history.
`wip/main` matters only when `main` has additional local work that is not ready
to become normal project history.

## Create a checkpoint

From the repository root in PowerShell:

```powershell
.\scripts\checkpoint_worktree.ps1 -Channel test -Preview
.\scripts\checkpoint_worktree.ps1 -Channel test -Push
```

Use `-Channel main` only while actually working on `main`.

The script uses an isolated temporary Git index. It does not switch branches,
alter the real staging area, clean files, reset files, or change the current
worktree. Each new checkpoint is a descendant of the prior checkpoint, so the
remote branch remains append-only and does not require a force push.

The public checkpoint intentionally excludes `.env` files, agent-local settings,
secret-like files, media, logs, coverage output, pytest scratch directories,
and ad-hoc repair scripts. It also scans for several high-confidence credential
signatures and refuses selected files larger than 25 MiB. Project recovery
notes are included only after personal paths have been removed.

## When to checkpoint

- Before a bulk rewrite, generated-build replacement, deletion, or risky agent
  operation.
- After each completed implementation milestone.
- At the end of every agent task that leaves meaningful uncommitted changes.
- Before switching branches or beginning a separate feature.

Checkpointing every coherent milestone is preferred over checkpointing every
individual file save. It produces useful recovery points without excessive
GitHub Actions activity or unreadable history.

## Recover safely

Inspect the checkpoint first:

```powershell
git fetch origin
git log --oneline origin/wip/test
git diff --stat HEAD origin/wip/test
```

For substantial recovery, create a separate worktree or recovery branch from
the checkpoint. Do not reset a dirty working tree to the checkpoint.

## Final project history

Checkpoint commits are safety history, not release history. Finished work still
goes through the normal feature branch and pull-request workflow. WIP commits
can be squash-merged so `main` remains concise.
