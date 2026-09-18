import re

with open('docs/DISC_ROUTING_TEST_WORKTREE_PLAN.md', 'r', encoding='utf-8') as f:
    text = f.read()

# Update M6 checkbox
text = text.replace('- [ ] M6: Visibility and User Fallback.', '- [x] M6: Visibility and User Fallback.')
text = text.replace('- [ ] Display the user\'s hint separately', '- [x] Display the user\'s hint separately')
text = text.replace('- [ ] Offer a metadata-only reassessment', '- [x] Offer a metadata-only reassessment')

# Add progress entry
text += '''- 2026-09-17: Completed M6's visibility and user fallback requirements.
  Extended `PipelineItemResponse` to export `user_hint`, `assessed_composition`,
  `assessed_role`, `current_route`, `evidence_status`, and `exhausted_reason`.
  Updated `RipPipelineView` to display these cleanly without misrepresenting
  model guesses as verified identity. Implemented `/pipeline/discs/{fingerprint}/reassess`
  for metadata-only legacy disc reassessment that safely appends a revision without
  re-reading physical media or altering completed assignments.
  Frontend compilation, Ruff checks, and pytest suite passed cleanly. No live
  operations occurred. Next: Hand-off/Completion.
'''

with open('docs/DISC_ROUTING_TEST_WORKTREE_PLAN.md', 'w', encoding='utf-8') as f:
    f.write(text)
