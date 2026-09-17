import sys

new_test = \"\"\"
def test_automatic_gemini_route_aborts_when_paused_or_stopped(tmp_path, monkeypatch):
    fingerprint = "0123456789abcdef"
    store = PipelineQueueStore(tmp_path / "queue.sqlite3")
    assessment = store.routing_append(
        DiscAssessment(
            fingerprint,
            (0, 1),
            user_hint="movie",
            evidence=(
                TitleEvidence(0, "database", "supported", "tv"),
                TitleEvidence(1, "database", "supported", "tv"),
            ),
        ),
        expected_revision=0,
    )
    for title_index in (0, 1):
        media_id = f"disc-01-title-00{title_index}"
        contract = tmp_path / f"{media_id}.json"
        contract.write_text(
            json.dumps({
                "mode": "verified-rip-contract",
                "disc_fingerprint": fingerprint,
                "title_index": title_index,
                "media_context": {
                    "routing_assessment": assessment.to_dict(),
                    "routing_assessment_digest": assessment.digest,
                    "routing_assessment_revision": assessment.revision,
                },
            }),
            encoding="utf-8",
        )
        store.enqueue_verified_rip(media_id, build_artifact("rip", contract))
        store.hold_for_review(media_id, "tv_title_no_match")
    
    monkeypatch.setattr(
        "mkv_episode_matcher.backend.downstream_worker.get_config_manager",
        lambda: SimpleNamespace(
            load=lambda: SimpleNamespace(automatic_processing_enabled=True)
        ),
    )
    
    worker = DownstreamWorker(
        SimpleNamespace(store=store, stop_event=SimpleNamespace(is_set=lambda: False)),
        allowed_stages=("identify",)
    )
    
    assert worker._settle_terminal_tv_route() is True
    assert worker._settle_terminal_tv_route() is True
    
    store.set_paused(True)
    assert worker._apply_automatic_assessed_gemini_route() is False
    
    store.set_paused(False)
    worker.dispatcher.stop_event = SimpleNamespace(is_set=lambda: True)
    assert worker._apply_automatic_assessed_gemini_route() is False
\"\"\"

with open("tests/test_downstream_worker.py", "a") as f:
    f.write("\n" + new_test + "\n")
