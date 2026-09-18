import re

with open('tests/test_rip_drive_prepare.py', 'r', encoding='utf-8') as f:
    code = f.read()

def replace_get_engine(match):
    return '''    monkeypatch.setattr("mkv_episode_matcher.backend.routers.rip.get_engine", lambda: type("e", (), {"asr": None})())
    monkeypatch.setattr("mkv_episode_matcher.backend.routers.rip.execute_gemini_fallback", lambda store, ids, *a: [ids[0]])'''

code = re.sub(
    r'    monkeypatch\.setattr\("mkv_episode_matcher\.backend\.routers\.rip\.execute_gemini_fallback", lambda store, ids, \*a: \[ids\[0\]\]\)',
    replace_get_engine,
    code,
    flags=re.MULTILINE
)

with open('tests/test_rip_drive_prepare.py', 'w', encoding='utf-8') as f:
    f.write(code)
