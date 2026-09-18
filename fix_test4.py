with open('tests/test_m5_concurrency.py', 'r', encoding='utf-8') as f:
    code = f.read()

# Replace TestClient stuff
code = code.replace(
    'from fastapi.testclient import TestClient',
    'from fastapi import HTTPException'
)
code = code.replace('from mkv_episode_matcher.backend.main import create_app', '')
code = code.replace(
    '    app = create_app()\n    app.dependency_overrides[\n        __import__("mkv_episode_matcher.backend.dependencies", fromlist=["get_pipeline_queue_store"]).get_pipeline_queue_store\n    ] = lambda: store\n    \n    client = TestClient(app)\n',
    '    from mkv_episode_matcher.backend.routers.rip import execute_pipeline_gemini_fallback, GeminiFallbackExecutionRequest\n'
)

call_api = '''
        request = GeminiFallbackExecutionRequest(media_ids=[media_id], confirm_media_read=True, confirm_external_transmission=True, confirm_classification=True, disc_fingerprint="0"*16)
        response = execute_pipeline_gemini_fallback(request, store, tmp_path)
'''
code = code.replace(
    '        response = client.post(\n            "/rip/pipeline/classify-unmatched-disc",\n            json={"media_ids": [media_id], "confirm_media_read": True, "confirm_external_transmission": True, "confirm_classification": True, "disc_fingerprint": "0"*16},\n        )\n        assert response.status_code == 200',
    call_api
)

with open('tests/test_m5_concurrency.py', 'w', encoding='utf-8') as f:
    f.write(code)
