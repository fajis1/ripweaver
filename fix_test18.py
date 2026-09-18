with open('tests/test_downstream_worker.py', 'r', encoding='utf-8') as f:
    code = f.read()

code = code.replace(
    'mkv_episode_matcher.backend.downstream_worker.execute_gemini_fallback',
    'mkv_episode_matcher.backend.gemini_fallback.execute_gemini_fallback'
)

with open('tests/test_downstream_worker.py', 'w', encoding='utf-8') as f:
    f.write(code)
