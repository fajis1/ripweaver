with open('tests/test_downstream_worker.py', 'r', encoding='utf-8') as f:
    code = f.read()

code = code.replace(
    'media_id = "test-media"\n    contract = tmp_path / f"{media_id}.json"',
    '    media_id = "test-media"\n    contract = tmp_path / f"{media_id}.json"'
)

with open('tests/test_downstream_worker.py', 'w', encoding='utf-8') as f:
    f.write(code)
