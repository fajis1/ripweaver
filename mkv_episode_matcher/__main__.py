import sys

from mkv_episode_matcher.cli import app as cli_app
from mkv_episode_matcher.cli import serve

_PORTABLE_SMOKE_ARGUMENT = "--portable-smoke-test"


def main():
    """Entry point for the application.

    If no arguments are provided, defaults to launching the web server.
    This makes the executable user-friendly when double-clicked.
    """
    if sys.argv[1:] == [_PORTABLE_SMOKE_ARGUMENT]:
        from mkv_episode_matcher.portable_smoke import run_portable_smoke_check

        result = run_portable_smoke_check()
        print(
            "RipWeaver portable smoke test passed "
            f"(version={result.version}, assets={result.asset_count})"
        )
        return

    # If no arguments provided (e.g., double-clicking the exe), default to serve
    if len(sys.argv) == 1:
        # Call serve directly with defaults
        serve(
            port=8001,
            host="127.0.0.1",
            no_browser=False,
            hold_automatic_rips=False,
        )
    else:
        cli_app()


if __name__ == "__main__":
    main()
