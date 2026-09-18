with open('tests/test_downstream_worker.py', 'r', encoding='utf-8') as f:
    code = f.read()

code = code.replace(
    'monkeypatch.setattr("mkv_episode_matcher.backend.downstream_worker.get_engine"',
    'monkeypatch.setattr("mkv_episode_matcher.backend.dependencies.get_engine"'
)
code = code.replace(
    'monkeypatch.setattr("mkv_episode_matcher.backend.downstream_worker.get_pipeline_contract_root"',
    'monkeypatch.setattr("mkv_episode_matcher.backend.dependencies.get_pipeline_contract_root"'
)

# And in test_manual_endpoints_queue_transition_lock:
code = code.replace(
    'monkeypatch.setattr("mkv_episode_matcher.backend.routers.rip.get_engine"',
    'monkeypatch.setattr("mkv_episode_matcher.backend.dependencies.get_engine"'
)
code = code.replace(
    'monkeypatch.setattr("mkv_episode_matcher.backend.routers.rip.get_pipeline_contract_root"',
    'monkeypatch.setattr("mkv_episode_matcher.backend.dependencies.get_pipeline_contract_root"'
)

with open('tests/test_downstream_worker.py', 'w', encoding='utf-8') as f:
    f.write(code)
