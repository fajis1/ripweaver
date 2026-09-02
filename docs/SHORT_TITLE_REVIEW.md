# Short-title review

RipWeaver can hold unusually short ripped titles for review before episode
matching. This keeps menus, previews, logos, and other very short clips from
entering the matcher automatically while leaving the file under the user's
control.

## Configure the cutoff

Open **Settings**, then find **Media Pipeline Locations** and set **Short-title
review cutoff**.

- The default is **150 seconds (2 minutes 30 seconds)**.
- A title is held only when it is strictly shorter than the cutoff. A title
  exactly equal to the cutoff is not held.
- The accepted range is 0 through 3,600 seconds.
- Set the value to **0** to disable short-title review.

The selected cutoff is saved when the settings form is saved. Each newly
verified rip contract retains the cutoff that was active when it was created,
so changing the setting affects future contracts rather than silently changing
an item already being reviewed.

## Review choices

When a newly ripped title is shorter than the configured cutoff, RipWeaver
stops it before matching and displays these choices:

| Choice | Result |
| --- | --- |
| **Keep file · ignore matching** | Keeps the staged MKV, excludes it from episode matching, and remembers the decision for that exact disc and title. |
| **Use for matching** | Sends the title through the normal matching workflow and remembers that it should be included for that exact disc and title. |
| **Mark for deletion review** | Keeps the file, excludes it from matching, and records that it should be reviewed for deletion. It does not delete anything. |
| **Delete staged rip permanently** | Appears only after a staged file has been marked for deletion review. It requires a separate confirmation and permanently deletes only that staged rip. It does not delete a media-library file. |

The remembered decision is scoped to the exact disc fingerprint and title
index. It is not a broad rule applied to unrelated discs or files.

## Safety behavior

The initial review buttons never rip, transcode, move, delete, or eject media.
In particular, **Mark for deletion review** changes review metadata only. A
permanent deletion remains a separate, explicitly confirmed action and should
be tested only with disposable test media.

## How to test it

1. Restart the source-test copy of RipWeaver so it loads the current code.
2. Open **Settings**, set the short-title cutoff to 150 seconds or another test
   value, and save the settings.
3. Prepare a future test rip containing a title shorter than the cutoff.
4. Confirm that the title is held before matching and that the review panel
   shows the three non-destructive choices.
5. Test **Keep file · ignore matching** and **Use for matching** with safe test
   items and confirm the chosen disposition persists.
6. If testing deletion, use a disposable staged file, select **Mark for
   deletion review** first, and carefully review the separate permanent-delete
   confirmation.

Previously processed items whose contracts do not contain the new duration
metadata will not automatically gain the short-title review panel. The duration
for new items comes from the saved MakeMKV inventory; RipWeaver does not need to
open the media merely to decide whether the item is short.

## Development status

The feature was added in commit `f61aa37` on the `codex/support-bundle` branch.
The Python test suite passed with 1,159 tests, and the frontend lint and
production build completed successfully. It has not been merged into `main` or
included in a public installer or release yet.

See [Project status](PROJECT_STATUS.md) for the broader development record.
