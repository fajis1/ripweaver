import re

with open('mkv_episode_matcher/backend/downstream_worker.py', 'r', encoding='utf-8') as f:
    code = f.read()

def replace_return_true(m):
    return f'''print("RETURNING TRUE AT {m.start()}"); {m.group(0)}'''

code = re.sub(r'return True', replace_return_true, code)

with open('mkv_episode_matcher/backend/downstream_worker.py', 'w', encoding='utf-8') as f:
    f.write(code)
