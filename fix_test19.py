import sys
import unittest.mock as mock

with open('tests/test_downstream_worker.py', 'r', encoding='utf-8') as f:
    code = f.read()

replacement = '''
    class FakeEngine:
        asr = None
        gemini_fallback = None
        def execute_gemini_fallback(self, store, media_ids, *args, **kwargs):
            store.push('identify', 'new_contract.json', '0'*16, 1)
            return GeminiFallbackOutcome(errors=(), results=(GeminiTitleOutcome(media_id=media_ids[0], disposition="matched", accepted_role="movie"),))
            
    monkeypatch.setattr("mkv_episode_matcher.backend.dependencies.get_engine", lambda: FakeEngine())
    
    # We ALSO patch execute_gemini_fallback using mock.patch as a context manager for safety.
'''

# Wait, why not just mock get_engine() back, AND patch asr_model_name?

