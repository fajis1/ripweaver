with open('mkv_episode_matcher/backend/downstream_worker.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()
for i in range(len(lines)):
    if 'try:' in lines[i] and 'assessment = ' in lines[i+1]:
        print("".join(lines[i-5:i+15]))
        break
