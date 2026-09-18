import re

with open('mkv_episode_matcher/backend/downstream_worker.py', 'r', encoding='utf-8') as f:
    code = f.read()

replacement = '''                except Exception as e:
                    print(f"ENTERED EXCEPT: {type(e)} {e}, settled={settled}")
                    logger.exception("Automatic Gemini title route held safely")
                    if settled:
                        print("HITTING CONTINUE")
                        continue'''

code = code.replace('''                except Exception:
                    logger.exception("Automatic Gemini title route held safely")
                    if settled:
                        continue''', replacement)

with open('mkv_episode_matcher/backend/downstream_worker.py', 'w', encoding='utf-8') as f:
    f.write(code)
