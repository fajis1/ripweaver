import sys
from pathlib import Path
from mkv_episode_matcher.pipeline_queue import PipelineQueueStore
store = PipelineQueueStore(Path(sys.argv[1]))
attempts = store.routing_attempts("0123456789abcdef", 0)
for a in attempts: print(a)
