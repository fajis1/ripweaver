with open('tests/test_downstream_worker.py', 'r', encoding='utf-8') as f:
    code = f.read()

code = code.replace(
    'mkv_episode_matcher.backend.downstream_worker.get_pipeline_queue_store',
    'mkv_episode_matcher.backend.dependencies.get_pipeline_queue_store'
)

with open('tests/test_downstream_worker.py', 'w', encoding='utf-8') as f:
    f.write(code)
