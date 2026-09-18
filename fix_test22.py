import re

with open('tests/test_downstream_worker.py', 'r', encoding='utf-8') as f:
    code = f.read()

code = code.replace(
'''GeminiFallbackOutcome(errors=(), results=(GeminiTitleOutcome(media_id=media_ids[0], disposition="matched", accepted_role="movie"),))''',
'''GeminiFallbackOutcome(handled_ids=media_ids, titles=(GeminiTitleOutcome(media_id=media_ids[0], disposition="matched", accepted_role="movie"),))'''
)

with open('tests/test_downstream_worker.py', 'w', encoding='utf-8') as f:
    f.write(code)
