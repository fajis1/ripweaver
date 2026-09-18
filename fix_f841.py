import re

# fix test_downstream_worker.py
with open('tests/test_downstream_worker.py', 'r', encoding='utf-8') as f:
    text = f.read()
text = text.replace('response = execute_pipeline_gemini_fallback(request, store, tmp_path)', 'execute_pipeline_gemini_fallback(request, store, tmp_path)')
with open('tests/test_downstream_worker.py', 'w', encoding='utf-8') as f:
    f.write(text)

# fix test_engine_v2.py
with open('tests/test_engine_v2.py', 'r', encoding='utf-8') as f:
    text = f.read()
text = text.replace('show_name, season = engine._detect_context(test_file)', 'engine._detect_context(test_file)')
text = text.replace('results, _ = engine.process_path(test_file, tmdb_id=549, dry_run=True)', 'engine.process_path(test_file, tmdb_id=549, dry_run=True)')
text = text.replace('os_called = mock_dependencies[', 'mock_dependencies[')
with open('tests/test_engine_v2.py', 'w', encoding='utf-8') as f:
    f.write(text)

# fix test_tmdb_id_feature.py
with open('tests/test_tmdb_id_feature.py', 'r', encoding='utf-8') as f:
    text = f.read()
text = text.replace('result = provider.get_subtitles(', 'provider.get_subtitles(')
with open('tests/test_tmdb_id_feature.py', 'w', encoding='utf-8') as f:
    f.write(text)
