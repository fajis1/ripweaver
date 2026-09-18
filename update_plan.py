import re

with open('docs/DISC_ROUTING_TEST_WORKTREE_PLAN.md', 'r', encoding='utf-8') as f:
    text = f.read()

text = text.replace('1260 tests', '1263 tests')

with open('docs/DISC_ROUTING_TEST_WORKTREE_PLAN.md', 'w', encoding='utf-8') as f:
    f.write(text)
