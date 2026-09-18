import re

with open('tests/test_downstream_worker.py', 'r', encoding='utf-8') as f:
    code = f.read()

replacement = '''      def fake_execute_gemini_fallback(store, media_ids, *args, **kwargs):
          print("FAKE EXECUTE CALLED")
          contract_path = tmp_path / f"{media_ids[0]}_new.json"
          contract_path.write_text("{}", encoding="utf-8")
          from mkv_episode_matcher.pipeline_queue import build_artifact
          store.apply_reviewed_identification_input(media_ids[0], build_artifact("rip", contract_path))
          return GeminiFallbackOutcome(handled_ids=media_ids, titles=(GeminiTitleOutcome(media_id=media_ids[0], disposition="matched", accepted_role="movie"),))
'''

code = re.sub(
    r'      def fake_execute_gemini_fallback\(store, media_ids, \*args, \*\*kwargs\):\n.*?accepted_role="movie"\),\)\)',
    replacement,
    code,
    flags=re.DOTALL
)

with open('tests/test_downstream_worker.py', 'w', encoding='utf-8') as f:
    f.write(code)
