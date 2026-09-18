import re

with open('mkv_episode_matcher/frontend/src/components/RipPipelineView.tsx', 'r', encoding='utf-8') as f:
    text = f.read()

text = text.replace('await fetchQueue();', '')

with open('mkv_episode_matcher/frontend/src/components/RipPipelineView.tsx', 'w', encoding='utf-8') as f:
    f.write(text)
