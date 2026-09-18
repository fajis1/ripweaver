import re

with open('docs/DISC_ROUTING_TEST_WORKTREE_PLAN.md', 'r', encoding='utf-8') as f:
    text = f.read()

# I will append a note about fixing the manual endpoint test race conditions and the drive prepare FakeThread kwargs
new_text = text.replace(
    '- Worker ruff import-order errors have been fixed. The full synthetic test suite (1260 tests) and ruff lints pass!',
    '- Worker ruff import-order errors have been fixed. The full synthetic test suite (1260 tests) and ruff lints pass!\n    - **Update (Later on 2026-09-17):** Discovered and fixed race conditions in the manual endpoints queue transition test (	est_manual_endpoints_queue_transition_lock) and 	est_gemini_retry_starts_only_the_exact_requested_item. The mock thread behaviors were corrected to allow deterministic queue transition assertions without executing unmocked functions (like loading the ASR model) or escaping the monkeypatch context. The full suite passes reliably.'
)

with open('docs/DISC_ROUTING_TEST_WORKTREE_PLAN.md', 'w', encoding='utf-8') as f:
    f.write(new_text)
