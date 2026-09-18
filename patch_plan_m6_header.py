# coding=utf-8
import re

with open('docs/DISC_ROUTING_TEST_WORKTREE_PLAN.md', 'r', encoding='utf-8') as f:
    text = f.read()

text = re.sub(r'M0[^a-zA-Z0-9]+M5: \*\*complete in synthetic validation; M6 pending\*\*', 'M0-M6: **complete in synthetic validation; M7 pending**', text)
text = text.replace('## Current agent handoff (2026-09-17, M5 complete, M6 pending)', '## Current agent handoff (2026-09-17, M6 complete, M7 pending)')
text = text.replace('- **Current agent handoff (2026-09-17, M5 complete, M6 pending):**', '- **Current agent handoff (2026-09-17, M6 complete, M7 pending):**')

# Remove trailing blank lines
text = re.sub(r'\n{3,}', '\n\n', text)
text = text.rstrip() + '\n'

with open('docs/DISC_ROUTING_TEST_WORKTREE_PLAN.md', 'w', encoding='utf-8') as f:
    f.write(text)
