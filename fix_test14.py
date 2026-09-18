with open('tests/test_downstream_worker.py', 'r', encoding='utf-8') as f:
    code = f.read()

replacement = '''
        def execute_gemini_fallback(self, store, media_ids, *args, **kwargs):
            store.push('identify', 'some_new_contract.json', '0'*16, 1)
            return type("GeminiFallbackOutcome", (), {"errors": (), "results": (GeminiTitleOutcome(media_id=media_ids[0], disposition="matched", accepted_role="movie"),)})()
'''
code = code.replace(
    '''
        def execute_gemini_fallback(self, *args, **kwargs):
            return type("GeminiFallbackOutcome", (), {"errors": (), "results": (GeminiTitleOutcome(media_id=media_id, disposition="matched", accepted_role="movie"),)})()
''',
    replacement
)

with open('tests/test_downstream_worker.py', 'w', encoding='utf-8') as f:
    f.write(code)
