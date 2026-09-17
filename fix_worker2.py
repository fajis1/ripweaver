import re

with open('mkv_episode_matcher/backend/downstream_worker.py', 'r', encoding='utf-8') as f:
    code = f.read()

# 1. Add settled = False inside the try
code = code.replace(
    '                try:\n                    store.choose_review_path',
    '                settled = False\n                try:\n                    store.choose_review_path'
)

# 2. Set settled = True when routing_settle is called successfully for matched and review
code = code.replace(
    '                    if outcome == "matched":\n                        from dataclasses import replace\n\n                        from mkv_episode_matcher.disc.routing import TitleEvidence\n\n                        store.routing_settle(assessment, title_index, route, outcome)',
    '                    if outcome == "matched":\n                        from dataclasses import replace\n\n                        from mkv_episode_matcher.disc.routing import TitleEvidence\n\n                        store.routing_settle(assessment, title_index, route, outcome)\n                        settled = True'
)

code = code.replace(
    '                    else:\n                        store.routing_settle(assessment, title_index, route, outcome)',
    '                    else:\n                        store.routing_settle(assessment, title_index, route, outcome)\n                        settled = True'
)

# 3. Check settled in except Exception
code = code.replace(
    '                except Exception:\n                    logger.exception("Automatic Gemini title route held safely")\n                    current = store.get(item.media_id)',
    '                except Exception:\n                    logger.exception("Automatic Gemini title route held safely")\n                    if settled:\n                        continue\n                    current = store.get(item.media_id)'
)

with open('mkv_episode_matcher/backend/downstream_worker.py', 'w', encoding='utf-8') as f:
    f.write(code)
