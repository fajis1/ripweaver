with open('tests/test_downstream_worker.py', 'r', encoding='utf-8') as f:
    code = f.read()

replacement = '''
    def fake_execute_gemini_fallback(store, media_ids, *args, **kwargs):
        store.push('identify', 'new_contract.json', '0'*16, 1)
        return GeminiFallbackOutcome(errors=(), results=(GeminiTitleOutcome(media_id=media_ids[0], disposition="matched", accepted_role="movie"),))
    monkeypatch.setattr("mkv_episode_matcher.backend.downstream_worker.execute_gemini_fallback", fake_execute_gemini_fallback)
'''

code = code.replace('''
    class FakeEngine:
        asr = None
        gemini_fallback = None
        def execute_gemini_fallback(self, store, media_ids, *args, **kwargs):
            # simulate pushing a new contract so we pass the current.artifact.contract_path check
            store.push('identify', 'new_contract.json', '0'*16, 1)
            return GeminiFallbackOutcome(errors=(), results=(GeminiTitleOutcome(media_id=media_ids[0], disposition="matched", accepted_role="movie"),))
            
    monkeypatch.setattr("mkv_episode_matcher.backend.dependencies.get_engine", lambda: FakeEngine())
''', replacement)

code = code.replace('''
    class FakeEngine:
        asr = None
        gemini_fallback = None
        def execute_gemini_fallback(self, store, media_ids, *args, **kwargs):
            store.push('identify', 'new_contract.json', '0'*16, 1)
            return GeminiFallbackOutcome(errors=(), results=(GeminiTitleOutcome(media_id=media_ids[0], disposition="matched", accepted_role="movie"),))
            
    monkeypatch.setattr("mkv_episode_matcher.backend.dependencies.get_engine", lambda: FakeEngine())
''', replacement)

with open('tests/test_downstream_worker.py', 'w', encoding='utf-8') as f:
    f.write(code)
