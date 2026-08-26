# RipPipelineView.tsx Recovery Guide

## Incident Summary
On 2026-08-23, a `git checkout` command was accidentally executed on `mkv_episode_matcher/frontend/src/components/RipPipelineView.tsx` during a debugging session. This reverted the file to its last committed state (`5150c24`), permanently deleting approximately 1,200 lines of **uncommitted** React code.

All backend code, tests, and other frontend files were unaffected.

## What Was Lost
The lost code primarily implemented the **"Existing rip recovery" UI**. This includes:
- Complex state derivations identifying previously successful/failed disc rips (`historicallyKnownTitleIndexes`, `expectedPipelineTitleIndexes`, `unavailableInventoryTitleIndexes`, etc.).
- UI components displaying the "Current recovery status" (e.g., "0 of 7 titles are already safely present in staging or Jellyfin").
- UI recommendations directing the user to skip certain titles or re-rip missing ones.
- Modified drive job selection priority (e.g., updates to the `latestJobForDrive` callback to handle `superseded` states and `completed` jobs properly).

## Where to Find the Surviving Code

Although the raw `.tsx` source was lost, the logic survives in two places:

### 1. The Partial Source Code (Recovered Snippets)
Before the file was reverted, an agent read portions of the file. Those exact source code lines (603 lines) have been extracted from internal logs and saved here:
**`%USERPROFILE%\.gemini\antigravity-cli\brain\7235cd07-d16c-4879-a283-8188b8e1afd0\scratch\recovered_lines.tsx`**

This file contains exact line numbers and original source code for critical areas, including the `historicallyKnownTitleIndexes` logic block, the "Current recovery status" JSX rendering, and the drive card UI.

### 2. The Full Compiled Logic
Just prior to the data loss, `npm run build` was executed successfully. The complete, fully-functional logic of the missing 1,200 lines exists in the minified production build:
**`mkv_episode_matcher/frontend/dist/assets/index-BEG82u0d.js`**

*Note: You can search this file for strings like `"Current recovery status"` to locate the minified React component. The variables are mangled (e.g., single letters), but the exact conditional logic, DOM structure, and component architecture are perfectly preserved.*

## Instructions for Reconstructing

If an agent is tasked with restoring `RipPipelineView.tsx`:
1. Open `RipPipelineView.tsx` (the current reverted version).
2. Open the `recovered_lines.tsx` file (path above). You can drop the recovered exact source code almost directly back into the file based on the line numbers provided.
3. For any gaps missing between the recovered snippets, read the minified `index-BEG82u0d.js` file, find the corresponding minified React elements, and reverse-engineer them back into standard JSX/TypeScript.
4. Ensure the `latestJobForDrive` hook is rebuilt to filter out superseded jobs correctly and prioritize attached executors.
5. Once reconstructed, run `npm run build` again and compare the new output to `index-BEG82u0d.js` to ensure logical parity.
