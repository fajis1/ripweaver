from datetime import datetime, timedelta

from mkv_episode_matcher.backend.routers.rip import _retained_source_status
from mkv_episode_matcher.core.datetime_compat import UTC
from mkv_episode_matcher.core.models import Config


def test_retained_source_status_distinguishes_active_and_expired(tmp_path):
    retained = tmp_path / "retained.mkv"
    retained.write_bytes(b"verified")
    contract = tmp_path / "contract.json"
    contract.write_text("{}", encoding="utf-8")

    active = _retained_source_status(
        retained_path=str(retained),
        retained_size=retained.stat().st_size,
        retained_at=(datetime.now(UTC) - timedelta(days=29)).isoformat(),
        contract_path=contract,
        ttl_days=30,
    )
    expired = _retained_source_status(
        retained_path=str(retained),
        retained_size=retained.stat().st_size,
        retained_at=(datetime.now(UTC) - timedelta(days=31)).isoformat(),
        contract_path=contract,
        ttl_days=30,
    )

    assert active == "active"
    assert expired == "expired"


def test_cleanup_postponement_is_normalized_to_utc():
    config = Config(retained_source_cleanup_postponed_until="2026-08-18T12:00:00-07:00")

    assert config.retained_source_cleanup_postponed_until == "2026-08-18T19:00:00+00:00"
