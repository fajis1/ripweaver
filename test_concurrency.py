import threading
import time
from dataclasses import replace
import pytest

from mkv_episode_matcher.backend.downstream_worker import DownstreamIdentificationWorker
from mkv_episode_matcher.core.config_manager import RipweaverConfig
from mkv_episode_matcher.disc.routing import DiscAssessment, TitleEvidence, RoutingError
from mkv_episode_matcher.pipeline_queue import PipelineQueueStore
from mkv_episode_matcher.backend.gemini_fallback import GeminiFallbackOutcome
from mkv_episode_matcher.disc.gemini_models import GeminiTitleMatch

def test_m5_sibling_revision_idempotent_retry(tmp_path, monkeypatch):
    \"\"\"
    Add tests where a sibling appends the same role, a different role, and repeated newer revisions during the settle/append gap.
    \"\"\"
    config_mock = RipweaverConfig(automatic_processing_enabled=True, automatic_gemini_ambiguity_fallback=True)
    monkeypatch.setattr(\"mkv_episode_matcher.backend.downstream_worker.get_config_manager\", lambda: type(\"m\", (), {\"load\": lambda: config_mock})())
    
    class FakeEngine:
        asr = None
    monkeypatch.setattr(\"mkv_episode_matcher.backend.downstream_worker.get_engine\", lambda: FakeEngine())
    monkeypatch.setattr(\"mkv_episode_matcher.backend.downstream_worker.get_pipeline_contract_root\", lambda: tmp_path)
    
    store = PipelineQueueStore(tmp_path / \"test.db\")
    store.initialize()
    
    # Create the item
    store.choose_review_path(store.push(\"identify\", \"some_path.mkv\", \"0\"*16, 1), \"gemini_evidence_required\")
    media_id = store.list_items()[0].media_id
    
    # Setup initial assessment
    initial_assessment = DiscAssessment(\"0\"*16, (1,))
    store.routing_claim_next(initial_assessment, 1) # claim route (e.g. 'classify')
    latest = store.routing_latest(\"0\"*16)
    
    # Mock execute_gemini_fallback
    monkeypatch.setattr(
        \"mkv_episode_matcher.backend.downstream_worker.execute_gemini_fallback\",
        lambda *args, **kwargs: GeminiFallbackOutcome(titles=[GeminiTitleMatch(media_id=media_id, disposition=\"matched\", accepted_role=\"movie\", reason=\"\")])
    )
    
    # Mock routing_append to simulate a sibling
    original_append = store.routing_append
    append_calls = 0
    
    def fake_append(assessment, expected_revision):
        nonlocal append_calls
        append_calls += 1
        
        if append_calls == 1:
            # Sibling appends a DIFFERENT role for a different title (or same title) to advance revision
            sibling = replace(latest, evidence=latest.evidence + (TitleEvidence(2, \"content\", \"supported\", \"extras\"),), revision=latest.revision + 1)
            original_append(sibling, expected_revision=latest.revision)
            # This will raise RoutingError because we expected latest.revision, but we just appended!
            # Actually, we can just raise RoutingError directly, but we also modify the DB!
            raise RoutingError(\"Sibling race!\")
        elif append_calls == 2:
            # Sibling appends the SAME role for the SAME title!
            current = store.routing_latest(\"0\"*16)
            sibling = replace(current, evidence=current.evidence + (TitleEvidence(1, \"content\", \"supported\", \"movie\"),), revision=current.revision + 1)
            original_append(sibling, expected_revision=current.revision)
            raise RoutingError(\"Sibling race 2!\")
            
        return original_append(assessment, expected_revision)
        
    monkeypatch.setattr(store, \"routing_append\", fake_append)
    
    worker = DownstreamIdentificationWorker(store)
    worker._apply_automatic_assessed_gemini_route()
    
    # Verify the item was processed and matched
    assert store.routing_attempts(\"0\"*16, 1)[-1].outcome == \"matched\"
    final_assessment = store.routing_latest(\"0\"*16)
    # The evidence should have 'extras' from the first race, and 'movie' from the second/retry!
    assert any(e.accepted_role == \"extras\" for e in final_assessment.evidence)
    assert any(e.accepted_role == \"movie\" and e.title_index == 1 for e in final_assessment.evidence)
    # It should not have DUPLICATE 'movie' roles for title 1!
    assert len([e for e in final_assessment.evidence if e.accepted_role == \"movie\" and e.title_index == 1]) == 1
    # Check that we called append multiple times
    assert append_calls >= 2

