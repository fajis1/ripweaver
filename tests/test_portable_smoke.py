import sys
from pathlib import Path

from mkv_episode_matcher import __main__ as entrypoint
from mkv_episode_matcher import portable_smoke

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_portable_smoke_app_serves_only_health_and_frontend():
    app = portable_smoke.build_portable_smoke_app()
    paths = {route.path for route in app.routes}

    assert "/health" in paths
    assert "/" in paths
    assert "/rip/jobs" not in paths


def test_portable_smoke_check_starts_and_stops_loopback_server():
    result = portable_smoke.run_portable_smoke_check(startup_timeout_seconds=10.0)

    assert result.version == portable_smoke.__version__
    assert result.asset_count >= 2


def test_double_click_entrypoint_uses_loopback(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "argv", ["RipWeaver.exe"])
    monkeypatch.setattr(
        entrypoint,
        "serve",
        lambda **kwargs: calls.append(kwargs),
    )

    entrypoint.main()

    assert calls == [
        {
            "port": 8001,
            "host": "127.0.0.1",
            "no_browser": False,
            "hold_automatic_rips": False,
        }
    ]


def test_portable_smoke_argument_bypasses_normal_server(monkeypatch, capsys):
    expected = portable_smoke.PortableSmokeResult(version="test-version", asset_count=2)
    monkeypatch.setattr(sys, "argv", ["RipWeaver.exe", "--portable-smoke-test"])
    monkeypatch.setattr(portable_smoke, "run_portable_smoke_check", lambda: expected)
    monkeypatch.setattr(
        entrypoint,
        "serve",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("normal server started")
        ),
    )

    entrypoint.main()

    assert "version=test-version" in capsys.readouterr().out


def test_release_workflow_preserves_reviewed_recovery_frontend():
    workflow = (
        _REPOSITORY_ROOT / ".github" / "workflows" / "build_release.yml"
    ).read_text(encoding="utf-8")

    assert "npm run build" not in workflow
    assert 'grep -R -F -q "Current recovery status"' in workflow
    assert "--portable-smoke-test" in workflow
