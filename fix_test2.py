with open('tests/test_m5_concurrency.py', 'r', encoding='utf-8') as f:
    code = f.read()

code = code.replace(
    'from mkv_episode_matcher.core.config_manager import RipweaverConfig',
    'class DummyConfig:\n    def __init__(self, **kwargs):\n        self.__dict__.update(kwargs)\n'
)
code = code.replace(
    'RipweaverConfig(automatic_processing_enabled=True, automatic_gemini_ambiguity_fallback=True)',
    'DummyConfig(automatic_processing_enabled=True, automatic_gemini_ambiguity_fallback=True)'
)

with open('tests/test_m5_concurrency.py', 'w', encoding='utf-8') as f:
    f.write(code)
