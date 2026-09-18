import os

with open('tests/test_m5_concurrency.py', 'r', encoding='utf-8') as f:
    m5_code = f.read()

# Fix the test names, imports, etc.
m5_code = m5_code.replace(
    'from mkv_episode_matcher.backend.downstream_worker import DownstreamIdentificationWorker',
    'from mkv_episode_matcher.backend.downstream_worker import DownstreamWorker'
)
m5_code = m5_code.replace('DownstreamIdentificationWorker', 'DownstreamWorker')

# Replace the DummyConfig hack with SimpleNamespace
m5_code = m5_code.replace(
    'class DummyConfig:\\n    def __init__(self, **kwargs):\\n        self.__dict__.update(kwargs)\\n',
    ''
)
m5_code = m5_code.replace(
    'DummyConfig(',
    'SimpleNamespace('
)
m5_code = "from types import SimpleNamespace\n" + m5_code

# Remove the import of GeminiTitleMatch and GeminiFallbackOutcome if needed, but let's just let it fail and fix it.
# Actually I already fixed GeminiTitleOutcome.
# Just append it to tests/test_downstream_worker.py
with open('tests/test_downstream_worker.py', 'a', encoding='utf-8') as f:
    f.write('\\n\\n')
    f.write(m5_code)

os.remove('tests/test_m5_concurrency.py')
