with open('mkv_episode_matcher/backend/routers/rip.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

new_lines = []
i = 0
while i < len(lines):
    # Check for all_season_analysis_running outer block
    if lines[i].strip() == 'for media_id in selected:' and 'store.choose_review_path(media_id, "all_season_analysis_running")' in lines[i+1]:
        i += 2
        continue
    # Check for gemini_analysis_running outer block (which has try/except)
    if lines[i].strip() == 'try:' and 'for media_id in selected:' in lines[i+1] and 'gemini_analysis_running' in lines[i+2]:
        i += 7
        continue

    if 'with _downstream_lock:' in lines[i]:
        new_lines.append(lines[i])
        if 'execute_unmatched_disc_analysis(' in ''.join(lines[i:i+10]):
            new_lines.append('            try:\n')
            new_lines.append('                for media_id in selected:\n')
            new_lines.append('                    store.choose_review_path(media_id, "all_season_analysis_running")\n')
            new_lines.append('            except PipelineQueueError:\n')
            new_lines.append('                return\n')
        elif 'execute_gemini_fallback(' in ''.join(lines[i:i+10]):
            new_lines.append('            try:\n')
            new_lines.append('                for media_id in selected:\n')
            new_lines.append('                    store.choose_review_path(media_id, "gemini_analysis_running")\n')
            new_lines.append('            except PipelineQueueError:\n')
            new_lines.append('                return\n')
        i += 1
        continue

    new_lines.append(lines[i])
    i += 1

with open('mkv_episode_matcher/backend/routers/rip.py', 'w', encoding='utf-8') as f:
    f.writelines(new_lines)
