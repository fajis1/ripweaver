with open('mkv_episode_matcher/backend/downstream_worker.py', 'r', encoding='utf-8') as f:
    code = f.read()

# Add settled tracking
code = code.replace(
    '                    store.routing_settle(assessment, title_index, route, outcome)',
    '                    store.routing_settle(assessment, title_index, route, outcome)\n                    settled = True'
)
code = code.replace(
    '                except Exception:',
    '                except Exception:\n                    logger.exception("Automatic Gemini title route held safely")\n                    if locals().get("settled"):\n                        continue'
)
# Note: we need to clean up the existing logger.exception and store.routing_settle in the except block
