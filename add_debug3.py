import re

with open('mkv_episode_matcher/backend/downstream_worker.py', 'r', encoding='utf-8') as f:
    code = f.read()

replacement = '''                            try:
                                print(f"CALLING routing_append FOR {title_index}")
                                store.routing_append(
                                    new_assessment, expected_revision=current_latest.revision
                                )'''

code = code.replace('''                            try:
                                store.routing_append(
                                    new_assessment, expected_revision=current_latest.revision
                                )''', replacement)

with open('mkv_episode_matcher/backend/downstream_worker.py', 'w', encoding='utf-8') as f:
    f.write(code)
