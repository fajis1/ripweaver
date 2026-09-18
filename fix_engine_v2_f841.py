import re

with open('tests/test_engine_v2.py', 'r', encoding='utf-8') as f:
    text = f.read()

# Fix season
text = text.replace('engine._detect_context(test_file)\n\n                # Should detect season from filename\n                assert season == 2', '_, season = engine._detect_context(test_file)\n\n                # Should detect season from filename\n                assert season == 2')

# Fix os_called
text = re.sub(r'mock_dependencies\[\n\s+"opensubtitles"\n\s+\].return_value.get_subtitles.called\n', '', text)

with open('tests/test_engine_v2.py', 'w', encoding='utf-8') as f:
    f.write(text)
