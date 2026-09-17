with open('mkv_episode_matcher/backend/downstream_worker.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()
for i in range(len(lines)):
    if 'except Exception:' in lines[i]:
        print("".join(lines[i-2:i+20]))
        break
