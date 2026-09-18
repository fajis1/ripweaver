with open('tests/test_m5_concurrency.py', 'r', encoding='utf-8') as f:
    code = f.read()

code = code.replace(
    'from mkv_episode_matcher.disc.gemini_models import GeminiTitleMatch',
    'from mkv_episode_matcher.backend.gemini_fallback import GeminiTitleOutcome'
)
code = code.replace(
    'GeminiTitleMatch(media_id=media_id, disposition="matched", accepted_role="movie", reason="")',
    'GeminiTitleOutcome(media_id=media_id, disposition="matched", accepted_role="movie")'
)

with open('tests/test_m5_concurrency.py', 'w', encoding='utf-8') as f:
    f.write(code)
