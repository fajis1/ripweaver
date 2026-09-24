import json
import sqlite3

from mkv_episode_matcher.disc.private_bindings import PrivateBindingStore
from mkv_episode_matcher.disc.rip_manifest import MediaContext


def test_private_binding_reads_legacy_routing_composition(tmp_path):
    report = tmp_path / "inventory.json"
    report.write_text("{}", encoding="utf-8")
    store = PrivateBindingStore(tmp_path / "private-bindings.sqlite3")
    context = MediaContext(
        disc_id="disc-01",
        series_name="Short Circuit 2",
        content_hint="movie",
    )
    store.bind(
        job_id="rip-0123456789abcdef0123456789abcdef",
        plan_sha256="a" * 64,
        report_paths=[report],
        output_root=tmp_path,
        media_contexts={context.disc_id: context},
    )

    database = sqlite3.connect(tmp_path / "private-bindings.sqlite3")
    row = database.execute(
        "SELECT media_contexts_json FROM private_bindings WHERE job_id = ?",
        ("rip-0123456789abcdef0123456789abcdef",),
    ).fetchone()
    payload = json.loads(row[0])
    payload["disc-01"]["routing_composition"] = "movies_with_extras"
    database.execute(
        "UPDATE private_bindings SET media_contexts_json = ? WHERE job_id = ?",
        (json.dumps(payload), "rip-0123456789abcdef0123456789abcdef"),
    )
    database.commit()
    database.close()

    restored = store.get("rip-0123456789abcdef0123456789abcdef")
    assert restored.media_contexts["disc-01"].content_hint == "movie"
