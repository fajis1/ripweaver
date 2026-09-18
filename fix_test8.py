import json

with open('tests/test_downstream_worker.py', 'r', encoding='utf-8') as f:
    code = f.read()

replacement = '''
    contract = tmp_path / f"{media_id}.json"
    contract.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "disc_fingerprint": "0"*16,
            "title_index": 1,
            "media_context": {},
        }),
        encoding="utf-8",
    )
    from mkv_episode_matcher.pipeline_queue import build_artifact
    store.enqueue_verified_rip(media_id, build_artifact("rip", contract))
'''
code = code.replace(
    'store.choose_review_path(store.push("identify", "some_path.mkv", "0"*16, 1), "gemini_evidence_required")',
    replacement + '    store.hold_for_review(media_id, "gemini_evidence_required")'
)

with open('tests/test_downstream_worker.py', 'w', encoding='utf-8') as f:
    f.write(code)
