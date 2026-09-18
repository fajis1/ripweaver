import re

with open('mkv_episode_matcher/backend/routers/rip.py', 'r', encoding='utf-8') as f:
    text = f.read()

text = text.replace('''        try:
            from mkv_episode_matcher.disc.routing_controller import next_route
            from mkv_episode_matcher.pipeline_queue import get_pipeline_queue_store
            store_instance = get_pipeline_queue_store()''', '''        try:
            from mkv_episode_matcher.disc.routing_controller import next_route
            # We already imported get_pipeline_queue_store globally
            store_instance = get_pipeline_queue_store()''')

with open('mkv_episode_matcher/backend/routers/rip.py', 'w', encoding='utf-8') as f:
    f.write(text)
