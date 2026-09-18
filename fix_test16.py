import re

with open('tests/test_downstream_worker.py', 'r', encoding='utf-8') as f:
    content = f.read()

def replacer(match):
    return '''
    initial_assessment = DiscAssessment("0"*16, (1,2,3))
    initial_assessment = store.routing_append(initial_assessment, expected_revision=0)

    contract = tmp_path / f"{media_id}.json"
    contract.write_text(
        json.dumps({
            "mode": "verified-rip-contract",
            "disc_fingerprint": "0"*16,
            "title_index": 1,
            "media_context": {
                "routing_assessment": initial_assessment.to_dict(),
                "routing_assessment_digest": initial_assessment.digest,
                "routing_assessment_revision": initial_assessment.revision
            },
        }),
        encoding="utf-8",
    )
    store.enqueue_verified_rip(media_id, build_artifact("rip", contract))
    store.hold_for_review(media_id, "gemini_evidence_required")
    media_id = store.list_items()[0].media_id
'''

# The pattern to match is from contract = tmp_path ... to initial_assessment = store.routing_append(...)
pattern = re.compile(r'    contract = tmp_path / f"\{media_id\}\.json".*?initial_assessment = store\.routing_append\(initial_assessment, expected_revision=0\)', re.DOTALL)

new_content = pattern.sub(replacer, content)

with open('tests/test_downstream_worker.py', 'w', encoding='utf-8') as f:
    f.write(new_content)
