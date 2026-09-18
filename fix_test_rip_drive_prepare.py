import re

with open('tests/test_rip_drive_prepare.py', 'r', encoding='utf-8') as f:
    code = f.read()

def replace_fake_thread(match):
    return '''    class FakeThread:
        def __init__(self, *, target, name, daemon):
            self.target = target

        def start(self):
            self.target()
            return None

    monkeypatch.setattr("mkv_episode_matcher.backend.routers.rip.execute_gemini_fallback", lambda store, ids, *a: [ids[0]])
'''

code = re.sub(
    r'    class FakeThread:\n        def __init__\(self, \*, target, name, daemon\):\n            self\.target = target\n\n        def start\(self\):\n            return None',
    replace_fake_thread,
    code,
    flags=re.MULTILINE
)

with open('tests/test_rip_drive_prepare.py', 'w', encoding='utf-8') as f:
    f.write(code)
