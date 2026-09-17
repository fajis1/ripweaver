from mkv_episode_matcher.backend.downstream_worker import _verified_gemini_route_assignment
import json
from types import SimpleNamespace

path = type("Path", (), {"read_text": lambda self, encoding: json.dumps({
    "media_context": {
        "special_feature_assignments": [
            {
                "title_index": 0,
                "classification": "matched-feature",
                "media_kind": "movie",
                "provisional_match": False
            }
        ]
    }
})})()
item = SimpleNamespace(artifact=SimpleNamespace(contract_path=path))

print("Res:", _verified_gemini_route_assignment(item, 0, "movie"))
