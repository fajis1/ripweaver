import json
with open("tests/test_downstream_worker.py", "r") as f:
    content = f.read()

old_fake_gemini = \"\"\"    def fake_gemini(_store, ids, _config, _asr, _root, *, return_outcomes):
        calls.append(ids)
        assert return_outcomes is True
        revised = tmp_path / "revised.json"
        revised.write_text(contract.read_text(encoding="utf-8"), encoding="utf-8")\"\"\"

new_fake_gemini = \"\"\"    def fake_gemini(_store, ids, _config, _asr, _root, *, return_outcomes):
        calls.append(ids)
        assert return_outcomes is True
        revised = tmp_path / "revised.json"
        revised_payload = json.loads(contract.read_text(encoding="utf-8"))
        revised_payload["media_context"]["special_feature_assignments"] = [{"title_index": 0, "classification": "matched-feature", "media_kind": "movie", "provisional_match": False}]
        revised.write_text(json.dumps(revised_payload), encoding="utf-8")\"\"\"

content = content.replace(old_fake_gemini, new_fake_gemini)

with open("tests/test_downstream_worker.py", "w") as f:
    f.write(content)
