with open('tests/test_m5_concurrency.py', 'r', encoding='utf-8') as f:
    code = f.read()

code = code.replace(
    'from mkv_episode_matcher.backend.routers.rip import _downstream_lock',
    'from mkv_episode_matcher.backend.automatic_rip import _downstream_lock'
)

with open('tests/test_m5_concurrency.py', 'w', encoding='utf-8') as f:
    f.write(code)
