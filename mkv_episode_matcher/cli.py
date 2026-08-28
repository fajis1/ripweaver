"""
Unified CLI interface for RipWeaver

This module provides a single, intuitive command-line interface that handles
all use cases with intelligent auto-detection and minimal configuration.
"""

import json
import sys
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

# Configure logger
logger.remove()
logger.add(sys.stderr, level="INFO")

from rich.table import Table

from mkv_episode_matcher.core.config_manager import get_config_manager
from mkv_episode_matcher.core.datetime_compat import UTC
from mkv_episode_matcher.core.engine import MatchEngineV2
from mkv_episode_matcher.core.models import Config

app = typer.Typer(
    name="mkv-match",
    help="""RipWeaver - Disc ripping, episode identification, and media organization

Quick Start:
  mkv-match serve     Launch the Web UI (recommended)
  mkv-match config    Configure settings interactively
  mkv-match match     Process files from command line""",
    no_args_is_help=True,
)

console = Console()


def _prompt_for_replacement_credential(error) -> bool:
    """Prompt interactively without echoing or logging a credential value."""

    from mkv_episode_matcher.core.credentials import (
        CREDENTIAL_SPECS,
        store_credential,
    )

    if not sys.stdin.isatty():
        return False

    spec = CREDENTIAL_SPECS[error.credential]
    console.print(f"[yellow]{spec.display_name} {error.reason}.[/yellow]")
    console.print(
        f"Get or manage it here: "
        f"[link={spec.management_url}]{spec.management_url}[/link]"
    )
    if not typer.confirm("Update this credential now?", default=True):
        return False

    value = typer.prompt(
        f"Paste the new {spec.display_name}",
        hide_input=spec.secret,
        show_default=False,
    )
    try:
        store_credential(error.credential, value)
    except ValueError as exc:
        console.print(f"[red]Credential was not updated: {exc}[/red]")
        return False

    console.print(
        "[green]Credential updated in the local, Git-ignored .env file.[/green]"
    )
    return True


def print_banner():
    """Print application banner."""
    banner = Text("RipWeaver", style="bold blue")
    console.print(
        Panel(banner, subtitle="Intelligent episode matching with zero-config setup")
    )


@app.command()
def match(
    path: Path = typer.Argument(
        ..., help="Path to MKV file, series folder, or entire library", exists=True
    ),
    # Core options
    season: int | None = typer.Option(
        None, "--season", "-s", help="Override season number for all files"
    ),
    reference_season: int | None = typer.Option(
        None,
        "--reference-season",
        min=0,
        max=99,
        help=(
            "Subtitle-provider season when it differs from the desired output season"
        ),
    ),
    show_name: str | None = typer.Option(
        None,
        "--show-name",
        help="Canonical show name override for generic staging folders",
    ),
    recursive: bool = typer.Option(
        True,
        "--recursive/--no-recursive",
        "-r/-nr",
        help="Search recursively in directories",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-d", help="Preview changes without renaming files"
    ),
    # Output options
    output_dir: Path | None = typer.Option(
        None,
        "--output-dir",
        "-o",
        help="Copy renamed files to this directory instead of renaming in place",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Output results in JSON format for automation"
    ),
    # Quality options
    confidence_threshold: float | None = typer.Option(
        None,
        "--confidence",
        "-c",
        min=0.0,
        max=1.0,
        help="Minimum confidence score for matches (0.0-1.0)",
    ),
    # Subtitle options
    download_subs: bool = typer.Option(
        True,
        "--download-subs/--no-download-subs",
        help="Automatically download subtitles if not found locally",
    ),
    # TMDB options
    tmdb_id: int | None = typer.Option(
        None,
        "--tmdb-id",
        help="Manually specify the TMDB Show ID (e.g. 549 for Law & Order)",
    ),
    # Logging options
    log_level: str = typer.Option(
        "INFO",
        "--log-level",
        "-l",
        help="Set logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
        case_sensitive=False,
    ),
):
    """
    Process MKV files with intelligent episode matching.

    Automatically detects whether you're processing:
    â€¢ A single file
    â€¢ A series folder
    â€¢ An entire library

    Examples:

        # Process a single file
        mkv-match episode.mkv

        # Process a series season
        mkv-match "/media/Breaking Bad/Season 1/"

        # Process entire library
        mkv-match /media/tv-shows/ --recursive

        # Dry run with custom output
        mkv-match episode.mkv --dry-run --output-dir ./renamed/

        # Automation mode
        mkv-match show/ --json --confidence 0.8
    """

    from mkv_episode_matcher.core.credentials import set_credential_recovery_handler

    set_credential_recovery_handler(
        None if json_output else _prompt_for_replacement_credential
    )

    # Configure logging level
    log_level = log_level.upper()
    valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    if log_level not in valid_levels:
        console.print(
            f"[red]Invalid log level: {log_level}. Must be one of {', '.join(valid_levels)}[/red]"
        )
        sys.exit(1)

    logger.remove()
    logger.add(sys.stderr, level=log_level)

    # Add file logging to the documented log directory
    try:
        _cm = get_config_manager()
        _cfg = _cm.load()
        _log_dir = _cfg.cache_dir.parent / "logs"
        _log_dir.mkdir(parents=True, exist_ok=True)
        logger.add(
            str(_log_dir / "mkv-match.log"),
            rotation="10 MB",
            retention="1 week",
            level=log_level,
            encoding="utf-8",
        )
    except Exception:
        pass  # Fall back to stderr-only if config isn't available yet

    if not json_output:
        print_banner()

    # Load configuration
    try:
        cm = get_config_manager()
        config = cm.load()

        # Override config with CLI options
        if confidence_threshold is not None:
            config.min_confidence = confidence_threshold

        if not download_subs:
            config.sub_provider = "local"

    except Exception as e:
        if json_output:
            print(json.dumps({"error": f"Configuration error: {e}"}))
        else:
            console.print(f"[red]Configuration error: {e}[/red]")
        sys.exit(1)

    # Initialize engine
    try:
        engine = MatchEngineV2(config)
    except Exception as e:
        if json_output:
            print(json.dumps({"error": f"Engine initialization failed: {e}"}))
        else:
            console.print(f"[red]Failed to initialize engine: {e}[/red]")
        sys.exit(1)

    # Detect processing mode
    if path.is_file():
        mode = "single_file"
    elif path.is_dir():
        # Count MKV files to determine if it's a series or library
        mkv_count = len(list(path.rglob("*.mkv") if recursive else path.glob("*.mkv")))
        if mkv_count == 0:
            if json_output:
                print(json.dumps({"error": "No MKV files found"}))
            else:
                console.print("[yellow]No MKV files found[/yellow]")
            sys.exit(0)
        elif mkv_count <= 30:  # Arbitrary threshold
            mode = "series_folder"
        else:
            mode = "library"
    else:
        if json_output:
            print(json.dumps({"error": "Invalid path"}))
        else:
            console.print("[red]Invalid path[/red]")
        sys.exit(1)

    if not json_output:
        mode_descriptions = {
            "single_file": "Processing single file",
            "series_folder": "Processing series folder",
            "library": "Processing entire library",
        }
        console.print(f"[blue]{mode_descriptions[mode]}[/blue]: {path}")

        if dry_run:
            console.print("[yellow]DRY RUN MODE - No files will be renamed[/yellow]")

    # Process files
    try:
        results, failures = engine.process_path(
            path=path,
            season_override=season,
            show_name_override=show_name,
            recursive=recursive,
            dry_run=dry_run,
            output_dir=output_dir,
            json_output=json_output,
            confidence_threshold=confidence_threshold,
            tmdb_id=tmdb_id,
            reference_season=reference_season,
        )

        # Output results
        if json_output:
            output_data = {
                "mode": mode,
                "path": str(path),
                "total_matches": len(results),
                "total_failures": len(failures),
                "dry_run": dry_run,
                "results": json.loads(engine.export_results(results)),
                "failures": [
                    {
                        "original_file": str(f.original_file),
                        "reason": f.reason,
                        "confidence": f.confidence,
                    }
                    for f in failures
                ],
            }
            print(json.dumps(output_data, indent=2))
        else:
            # Rich console summary
            if results or failures:
                _display_comprehensive_summary(
                    results, failures, dry_run, output_dir, console
                )
            else:
                console.print("[yellow]No MKV files processed[/yellow]")

    except Exception as e:
        if json_output:
            print(json.dumps({"error": f"Processing failed: {e}"}))
        else:
            console.print(f"[red]Processing failed: {e}[/red]")
        sys.exit(1)


@app.command()
def credentials(
    provider: Annotated[
        str | None,
        typer.Argument(
            help=(
                "Credential to update: tmdb, opensubtitles-api, "
                "opensubtitles-username, opensubtitles-password, "
                "gemini-primary, or gemini-paid"
            )
        ),
    ] = None,
    migrate_legacy: Annotated[
        bool,
        typer.Option(
            "--migrate-legacy",
            help=(
                "Move credential fields from the user JSON config into local "
                ".env without displaying their values"
            ),
        ),
    ] = False,
):
    """Show credential status or securely update one value in local .env."""

    from mkv_episode_matcher.core.credentials import (
        CREDENTIAL_SPECS,
        credential_is_configured,
        migrate_credentials_from_json,
        store_credential,
    )

    if migrate_legacy:
        if provider is not None:
            console.print(
                "[red]Do not combine a credential name with --migrate-legacy.[/red]"
            )
            raise typer.Exit(code=2)
        config_path = Path.home() / ".mkv-episode-matcher" / "config.json"
        migrated = migrate_credentials_from_json(config_path)
        if migrated:
            labels = ", ".join(CREDENTIAL_SPECS[name].display_name for name in migrated)
            console.print(
                "[green]Moved legacy credential fields into the local, "
                "Git-ignored .env:[/green] "
                f"{labels}"
            )
        else:
            console.print("[yellow]No legacy JSON credential fields found.[/yellow]")
        return

    if provider is None:
        table = Table(title="Credential status (values are never displayed)")
        table.add_column("Credential")
        table.add_column("Configured")
        table.add_column("Management")
        for spec in CREDENTIAL_SPECS.values():
            status = (
                "[green]yes[/green]"
                if credential_is_configured(spec.name)
                else "[yellow]no[/yellow]"
            )
            table.add_row(spec.display_name, status, spec.management_url)
        console.print(table)
        return

    normalized = provider.strip().lower()
    if normalized not in CREDENTIAL_SPECS:
        choices = ", ".join(CREDENTIAL_SPECS)
        console.print(f"[red]Unknown credential '{provider}'. Choose: {choices}[/red]")
        raise typer.Exit(code=2)

    spec = CREDENTIAL_SPECS[normalized]
    console.print(
        f"Manage {spec.display_name}: "
        f"[link={spec.management_url}]{spec.management_url}[/link]"
    )
    value = typer.prompt(
        f"Paste {spec.display_name}",
        hide_input=spec.secret,
        show_default=False,
    )
    try:
        store_credential(spec.name, value)
    except ValueError as exc:
        console.print(f"[red]Credential was not updated: {exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print("[green]Saved to the local, Git-ignored .env file.[/green]")


@app.command()
def config(
    show_cache_dir: bool = typer.Option(
        False, "--show-cache-dir", help="Show current cache directory location"
    ),
    reset: bool = typer.Option(
        False, "--reset", help="Reset configuration to defaults"
    ),
):
    """
    Configure RipWeaver settings.

    Most settings are auto-configured, but you can customize:
    â€¢ Cache directory location
    â€¢ Default confidence thresholds
    â€¢ ASR model preferences
    """

    cm = get_config_manager()

    if show_cache_dir:
        config = cm.load()
        console.print(f"Cache directory: [blue]{config.cache_dir}[/blue]")
        return

    if reset:
        config = Config()  # Default config
        cm.save(config)
        console.print("[green]Configuration reset to defaults[/green]")
        return

    # Interactive configuration
    console.print(Panel("RipWeaver Configuration"))

    config = cm.load()

    # Cache directory
    current_cache = str(config.cache_dir)
    new_cache = typer.prompt(
        "Cache directory", default=current_cache, show_default=True
    )
    if new_cache != current_cache:
        config.cache_dir = Path(new_cache)

    # Confidence threshold
    current_confidence = config.min_confidence
    new_confidence = typer.prompt(
        "Minimum confidence threshold (0.0-1.0)",
        type=float,
        default=current_confidence,
        show_default=True,
    )
    if 0.0 <= new_confidence <= 1.0:
        config.min_confidence = new_confidence

    # ASR Model Selection
    console.print("\n[bold]ASR Model Configuration:[/bold]")

    try:
        from mkv_episode_matcher.core.model_registry import (
            DEFAULT_MODEL,
            get_leaderboard_url,
            get_model_info,
            list_recommended_models,
        )

        current_model = config.asr_model_name
        models = list_recommended_models()

        # Display available models
        console.print("\n  [dim]Recommended models:[/dim]")
        model_list = list(models.keys())
        for i, model_name in enumerate(model_list, 1):
            model_info = models[model_name]
            is_default = " [DEFAULT]" if model_name == DEFAULT_MODEL else ""
            is_current = " [CURRENT]" if model_name == current_model else ""
            gpu_req = "GPU required" if model_info["requires_gpu"] else "CPU-friendly"
            console.print(f"    {i}. {model_name}{is_default}{is_current}")
            console.print(
                f"       ({model_info['size_mb']}MB, {gpu_req}, {model_info['quality']} quality)"
            )

        console.print(f"\n  [dim]Browse more models: {get_leaderboard_url()}[/dim]")
        console.print(
            "  [dim]Enter a number (1-{}) or a custom HuggingFace model ID[/dim]".format(
                len(model_list)
            )
        )

        new_model = typer.prompt(
            "ASR model",
            default=current_model,
            show_default=True,
        )

        # Handle numeric selection
        if new_model.isdigit() and 1 <= int(new_model) <= len(model_list):
            new_model = model_list[int(new_model) - 1]

        if new_model.strip():
            config.asr_model_name = new_model.strip()
            # Use whisper provider (parakeet is deprecated)
            config.asr_provider = "whisper"

    except Exception as e:
        console.print(f"[yellow]Error loading model registry: {e}[/yellow]")
        # Fallback to simple prompt
        current_model = config.asr_model_name
        new_model = typer.prompt(
            "ASR model name",
            default=current_model,
            show_default=True,
        )
        if new_model.strip():
            config.asr_model_name = new_model.strip()

    # Subtitle provider
    current_sub = config.sub_provider
    new_sub = typer.prompt(
        "Subtitle provider (local/opensubtitles)",
        default=current_sub,
        show_default=True,
    )
    if new_sub in ["local", "opensubtitles"]:
        config.sub_provider = new_sub

    console.print(
        "\n[bold]Credentials:[/bold] API keys and passwords are loaded from "
        "the Git-ignored .env file. See .env.example for variable names."
    )

    # Save configuration
    cm.save(config)
    console.print("[green]Configuration saved successfully[/green]")


@app.command()
def info():
    """
    Show system information and available models.
    """
    console.print(Panel("RipWeaver - System Information"))

    # Configuration info
    try:
        cm = get_config_manager()
        config = cm.load()

        console.print("\n[bold]Current Configuration:[/bold]")
        console.print(f"  Cache directory: {config.cache_dir}")
        console.print(f"  ASR model: [cyan]{config.asr_model_name}[/cyan]")
        console.print(f"  Subtitle provider: {config.sub_provider}")
        console.print(f"  Confidence threshold: {config.min_confidence}")

    except Exception as e:
        console.print(f"[red]Error loading config: {e}[/red]")
        config = None

    # Model registry info
    try:
        from mkv_episode_matcher.core.model_registry import (
            get_leaderboard_url,
            get_model_info,
            is_model_downloaded,
            list_recommended_models,
        )

        console.print("\n[bold]Recommended ASR Models:[/bold]")
        models = list_recommended_models()

        for model_name, model_info in models.items():
            is_current = config and model_name == config.asr_model_name
            current_marker = " [CURRENT]" if is_current else ""
            downloaded = is_model_downloaded(model_name)
            status = (
                "[green]Downloaded[/green]"
                if downloaded
                else "[dim]Not downloaded[/dim]"
            )
            gpu_req = (
                "[yellow]GPU[/yellow]"
                if model_info["requires_gpu"]
                else "[green]CPU[/green]"
            )

            console.print(f"  â€¢ {model_name}{current_marker}")
            console.print(f"    {model_info['description']}")
            console.print(
                f"    Size: {model_info['size_mb']}MB | {gpu_req} | Quality: {model_info['quality']} | {status}"
            )

        console.print(f"\n[dim]Browse more models: {get_leaderboard_url()}[/dim]")
        console.print("[dim]Run 'mkv-match config' to change your model[/dim]")

    except Exception as e:
        console.print(f"[red]Error checking models: {e}[/red]")


@app.command()
def preflight(  # noqa: C901
    drive: Annotated[
        list[int] | None,
        typer.Option(
            "--drive",
            "-d",
            help="MakeMKV drive index to inspect; repeat to select multiple drives",
        ),
    ] = None,
    output_dir: Annotated[
        Path,
        typer.Option(
            "--output-dir",
            "-o",
            help="Directory for parsed JSON and raw robot-mode reports",
        ),
    ] = Path(".mkv-preflight"),
    makemkv_path: Annotated[
        Path | None,
        typer.Option(
            "--makemkv-path",
            help="Path to makemkvcon64.exe (otherwise use MAKEMKV_PATH or the default)",
        ),
    ] = None,
    minimum_length: Annotated[
        int,
        typer.Option(
            "--min-length",
            min=0,
            help=(
                "Minimum title length in seconds included in MakeMKV's inventory; "
                "executable plans require 0 so MakeMKV title indexes stay stable"
            ),
        ),
    ] = 0,
    timeout: Annotated[
        int,
        typer.Option(
            "--timeout",
            min=30,
            help="Maximum seconds allowed for each drive scan",
        ),
    ] = 300,
):
    """Inventory loaded optical discs without ripping or changing media files."""

    from datetime import datetime

    from mkv_episode_matcher.disc.preflight import (
        PreflightError,
        parse_disc_inventory,
        parse_drives,
        resolve_makemkv_path,
        run_info_command,
        sanitize_robot_output,
        write_inventory_report,
    )

    console.print(
        Panel(
            "READ-ONLY PREFLIGHT\n"
            "Allowed MakeMKV action: info\n"
            "No ripping, ejection, rename, move, or delete operations"
        )
    )

    try:
        executable = resolve_makemkv_path(makemkv_path)
        output_dir = output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        selected_results = {}
        if drive:
            requested = tuple(dict.fromkeys(drive))
            if any(index < 0 or index > 99 for index in requested):
                raise PreflightError("MakeMKV drive indexes must be between 0 and 99")
            loaded = []
            for drive_index in requested:
                console.print(
                    f"[blue]Inspecting selected drive {drive_index}...[/blue]"
                )
                result = run_info_command(
                    executable,
                    f"disc:{drive_index}",
                    minimum_length=minimum_length,
                    timeout_seconds=timeout,
                )
                matching_drive = next(
                    (
                        item
                        for item in parse_drives(result.stdout)
                        if item.index == drive_index
                    ),
                    None,
                )
                if matching_drive is None:
                    raise PreflightError(
                        "Selected MakeMKV drive was absent from its targeted inventory"
                    )
                if not matching_drive.has_disc:
                    raise PreflightError(
                        "Selected MakeMKV drive does not report a loaded disc"
                    )
                loaded.append(matching_drive)
                selected_results[drive_index] = result
        else:
            console.print("[blue]Discovering MakeMKV drives...[/blue]")
            discovery = run_info_command(
                executable,
                "disc:9999",
                timeout_seconds=min(timeout, 120),
            )
            (output_dir / "drive-discovery.robot.log").write_text(
                sanitize_robot_output(discovery.stdout)
                + (
                    "\n--- STDERR ---\n" + sanitize_robot_output(discovery.stderr)
                    if discovery.stderr
                    else ""
                ),
                encoding="utf-8",
            )
            detected = parse_drives(discovery.stdout)
            loaded = [item for item in detected if item.has_disc]

        if not loaded:
            console.print("[yellow]No selected drives report a loaded disc.[/yellow]")
            raise typer.Exit(code=0)

        drive_table = Table(title="Loaded discs selected for sequential inspection")
        drive_table.add_column("Index", justify="right")
        drive_table.add_column("Device")
        drive_table.add_column("Drive")
        drive_table.add_column("Disc label")
        for item in loaded:
            drive_table.add_row(
                str(item.index),
                item.device_name,
                item.drive_name,
                item.disc_name,
            )
        console.print(drive_table)

        summary_table = Table(title="Read-only preflight results")
        summary_table.add_column("Index", justify="right")
        summary_table.add_column("Disc")
        summary_table.add_column("Titles", justify="right")
        summary_table.add_column("Streams", justify="right")
        summary_table.add_column("Warnings", justify="right")
        summary_table.add_column("Result")

        manifest = {
            "mode": "read-only",
            "created_at": datetime.now(UTC).isoformat(),
            "inventories": [],
        }

        for item in loaded:
            result = selected_results.get(item.index)
            if result is None:
                console.print(
                    f"[blue]Inspecting drive {item.index} ({item.device_name}) "
                    f"- {item.disc_name}...[/blue]"
                )
                result = run_info_command(
                    executable,
                    f"disc:{item.index}",
                    minimum_length=minimum_length,
                    timeout_seconds=timeout,
                )
            inventory = parse_disc_inventory(result, item)
            json_path, robot_path = write_inventory_report(
                output_dir,
                inventory,
                result,
                minimum_length_seconds=minimum_length,
            )
            stream_count = sum(len(title.streams) for title in inventory.titles)
            result_label = (
                "[green]inventoried[/green]"
                if inventory.titles
                else "[yellow]no titles parsed[/yellow]"
            )
            summary_table.add_row(
                str(item.index),
                item.disc_name,
                str(len(inventory.titles)),
                str(stream_count),
                str(len(inventory.warnings)),
                result_label,
            )
            manifest["inventories"].append({
                "drive_index": item.index,
                "disc_name": item.disc_name,
                "return_code": result.return_code,
                "json_report": json_path.name,
                "robot_log": robot_path.name,
            })

        manifest_path = output_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        console.print(summary_table)
        console.print(f"[green]Reports saved to {output_dir}[/green]")
        console.print(f"[dim]Manifest: {manifest_path}[/dim]")
    except PreflightError as exc:
        console.print(f"[red]Preflight stopped safely: {exc}[/red]")
        raise typer.Exit(code=1) from exc


@app.command("plan-titles")
def plan_titles(
    reports: Annotated[
        list[Path],
        typer.Argument(
            help="Saved preflight inventory JSON file(s) to classify",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit machine-readable plan JSON",
        ),
    ] = False,
    expected_episodes: Annotated[
        int | None,
        typer.Option(
            "--expected-episodes",
            min=1,
            help="Expected number of individual episodes on each report",
        ),
    ] = None,
    expected_runtime: Annotated[
        float | None,
        typer.Option(
            "--expected-runtime",
            min=1,
            help="Typical episode runtime in minutes",
        ),
    ] = None,
    runtime_tolerance: Annotated[
        float,
        typer.Option(
            "--runtime-tolerance",
            min=0.1,
            help="Allowed difference from expected runtime, in minutes",
        ),
    ] = 5.0,
):
    """Recommend titles and diagnostic audio without accessing a disc."""

    from mkv_episode_matcher.disc.title_selector import (
        TitlePlanError,
        load_title_plan,
    )

    try:
        plans = [
            load_title_plan(
                report,
                report_id=f"report-{index}",
                expected_episode_count=expected_episodes,
                expected_runtime_seconds=(
                    round(expected_runtime * 60)
                    if expected_runtime is not None
                    else None
                ),
                runtime_tolerance_seconds=round(runtime_tolerance * 60),
            )
            for index, report in enumerate(reports, start=1)
        ]
    except TitlePlanError as exc:
        if json_output:
            typer.echo(
                json.dumps({
                    "mode": "plan-only",
                    "status": "error",
                    "error": str(exc),
                })
            )
        else:
            console.print(f"[red]Planning stopped safely: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "mode": "plan-only",
                    "plans": [plan.to_dict() for plan in plans],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return

    console.print(
        Panel(
            "PLAN ONLY\n"
            "Reads saved JSON reports; does not access a disc or media file.\n"
            "Contains no rip, rename, move, delete, eject, or transcode action."
        )
    )
    for plan in plans:
        table = Table(
            title=(
                f"{plan.report_id}: title recommendations "
                f"(warning count: {plan.warning_count})"
            )
        )
        for note in plan.planning_notes:
            console.print(f"[dim]{plan.report_id}: {note}[/dim]")
        table.add_column("Title", justify="right")
        table.add_column("Runtime", justify="right")
        table.add_column("Class")
        table.add_column("Select")
        table.add_column("Diagnostic audio")
        table.add_column("Reason")

        for decision in plan.decisions:
            duration = decision.title.duration_seconds
            runtime = (
                f"{duration // 60}:{duration % 60:02d}"
                if duration is not None
                else "unknown"
            )
            diagnostic = (
                str(decision.diagnostic_audio_stream)
                if decision.diagnostic_audio_stream is not None
                else "none"
            )
            table.add_row(
                str(decision.title.index),
                runtime,
                decision.classification,
                "[green]yes[/green]" if decision.selected else "[yellow]no[/yellow]",
                diagnostic,
                " ".join(decision.reasons),
            )
        console.print(table)


@app.command("plan-special-features")
def plan_special_features(
    report: Annotated[
        Path,
        typer.Argument(
            help="Saved preflight inventory JSON file",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    catalogue: Annotated[
        Path,
        typer.Argument(
            help="Reviewed provider-neutral special-feature catalogue JSON",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable path-redacted JSON"),
    ] = False,
    maximum_runtime_delta: Annotated[
        int,
        typer.Option(
            "--maximum-runtime-delta",
            min=15,
            help="Largest permitted catalogue/runtime difference in seconds",
        ),
    ] = 120,
):
    """Match saved disc titles to reviewed special features without media access."""

    from mkv_episode_matcher.disc.special_features import (
        SpecialFeaturePlanError,
        load_special_feature_plan,
    )

    try:
        plan = load_special_feature_plan(
            report,
            catalogue,
            report_id="report-1",
            maximum_runtime_delta=maximum_runtime_delta,
        )
    except SpecialFeaturePlanError as exc:
        if json_output:
            typer.echo(
                json.dumps({
                    "mode": "special-features-plan-only",
                    "status": "error",
                    "error": str(exc),
                })
            )
        else:
            console.print(f"[red]Planning stopped safely: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    if json_output:
        typer.echo(json.dumps(plan.to_dict(), indent=2, sort_keys=True))
        return

    console.print(
        Panel(
            "SPECIAL FEATURES PLAN ONLY\n"
            "Reads saved inventory and catalogue JSON; does not access a disc "
            "or media file.\n"
            "Contains no rip, rename, move, delete, eject, or transcode action."
        )
    )
    for note in plan.planning_notes:
        console.print(f"[dim]{note}[/dim]")

    table = Table(
        title=(
            f"{plan.report_id}: special-feature recommendations "
            f"(missing catalogue entries: {len(plan.missing_feature_ids)})"
        )
    )
    table.add_column("Title", justify="right")
    table.add_column("Runtime", justify="right")
    table.add_column("Class")
    table.add_column("Recommend")
    table.add_column("Matched feature")
    table.add_column("Jellyfin folder")
    table.add_column("Delta", justify="right")
    for decision in plan.decisions:
        duration = decision.title.duration_seconds
        runtime = (
            f"{duration // 60}:{duration % 60:02d}"
            if duration is not None
            else "unknown"
        )
        delta = (
            str(decision.runtime_delta_seconds)
            if decision.runtime_delta_seconds is not None
            else "-"
        )
        table.add_row(
            str(decision.title.index),
            runtime,
            decision.classification,
            (
                "[green]yes[/green]"
                if decision.recommended_for_rip
                else "[yellow]review[/yellow]"
            ),
            decision.matched_title or "-",
            decision.jellyfin_folder or "-",
            delta,
        )
    console.print(table)
    if plan.missing_feature_ids:
        console.print(
            "[yellow]Missing catalogue IDs: "
            + ", ".join(plan.missing_feature_ids)
            + "[/yellow]"
        )


@app.command("plan-special-feature-rip")
def plan_special_feature_rip(
    report: Annotated[
        Path,
        typer.Argument(
            help="Saved preflight inventory JSON file",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    catalogue: Annotated[
        Path,
        typer.Argument(
            help="Reviewed provider-neutral special-feature catalogue JSON",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    manifest_out: Annotated[
        Path,
        typer.Option(
            "--manifest-out",
            help="New path for the non-executable diagnostic-rip manifest",
        ),
    ],
    maximum_runtime_delta: Annotated[
        int,
        typer.Option(
            "--maximum-runtime-delta",
            min=15,
            help="Largest permitted catalogue/runtime difference in seconds",
        ),
    ] = 120,
):
    """Plan diagnostic special-feature staging without disc or media access."""

    from mkv_episode_matcher.disc.special_feature_manifest import (
        SpecialFeatureManifestError,
        build_diagnostic_special_feature_manifest,
        write_diagnostic_special_feature_manifest,
    )
    from mkv_episode_matcher.disc.special_features import (
        SpecialFeaturePlanError,
        load_special_feature_plan,
    )

    try:
        resolved_output = manifest_out.resolve(strict=False)
        if resolved_output in {
            report.resolve(strict=True),
            catalogue.resolve(strict=True),
        }:
            raise SpecialFeatureManifestError(
                "Diagnostic manifest must be distinct from its inputs"
            )
        plan = load_special_feature_plan(
            report,
            catalogue,
            report_id="report-1",
            maximum_runtime_delta=maximum_runtime_delta,
        )
        manifest = build_diagnostic_special_feature_manifest(plan)
        write_diagnostic_special_feature_manifest(manifest_out, manifest)
    except (SpecialFeaturePlanError, SpecialFeatureManifestError) as exc:
        console.print(f"[red]Diagnostic planning stopped safely: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    console.print(
        Panel(
            "SPECIAL-FEATURE DIAGNOSTIC RIP PLAN ONLY\n"
            "No drive is bound and no MakeMKV command exists in this manifest.\n"
            "A fresh preflight, reviewed execution manifest, and separate "
            "authorization are still required."
        )
    )
    table = Table(title="Diagnostic staging candidates")
    table.add_column("Job")
    table.add_column("Title", justify="right")
    table.add_column("Class")
    table.add_column("Audio")
    table.add_column("Evidence")
    for job in manifest.jobs:
        table.add_row(
            job.job_id,
            str(job.title_index),
            job.classification,
            job.audio_policy,
            ", ".join(job.evidence_after_rip),
        )
    console.print(table)
    for excluded in manifest.excluded_titles:
        console.print(
            f"[yellow]Title {excluded.title_index} excluded:[/yellow] {excluded.reason}"
        )
    console.print(
        f"[green]Non-executable diagnostic manifest saved: {manifest_out}[/green]"
    )


@app.command("bind-special-feature-rip")
def bind_special_feature_rip(
    diagnostic_manifest: Annotated[
        Path,
        typer.Argument(
            help="Reviewed non-executable diagnostic manifest",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    fresh_inventory: Annotated[
        Path,
        typer.Argument(
            help="Fresh saved preflight inventory for the same disc",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    diagnostic_sha256: Annotated[
        str,
        typer.Option(
            "--diagnostic-sha256",
            help="Exact reviewed SHA-256 of the diagnostic manifest",
        ),
    ],
    manifest_out: Annotated[
        Path,
        typer.Option(
            "--manifest-out",
            help="New path for the bound, still-unauthorized manifest",
        ),
    ],
):
    """Bind a diagnostic plan to a fresh saved scan without disc access."""

    from mkv_episode_matcher.disc.special_feature_binder import (
        SpecialFeatureBindError,
        bind_diagnostic_special_feature_manifest,
        write_bound_special_feature_manifest,
    )

    try:
        output = manifest_out.resolve(strict=False)
        if output in {
            diagnostic_manifest.resolve(strict=True),
            fresh_inventory.resolve(strict=True),
        }:
            raise SpecialFeatureBindError(
                "Bound manifest must be distinct from its inputs"
            )
        manifest = bind_diagnostic_special_feature_manifest(
            diagnostic_manifest,
            fresh_inventory,
            expected_diagnostic_sha256=diagnostic_sha256,
        )
        write_bound_special_feature_manifest(manifest_out, manifest)
    except SpecialFeatureBindError as exc:
        console.print(f"[red]Special-feature binding stopped safely: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    console.print(
        Panel(
            "SPECIAL-FEATURE FRESH-PREFLIGHT BINDING\n"
            "The diagnostic digest and complete inventory signature match.\n"
            "This manifest is still unauthorized and cannot be executed by "
            "the episode rip command."
        )
    )
    table = Table(title="Exactly bound special-feature titles")
    table.add_column("Job")
    table.add_column("Title", justify="right")
    table.add_column("Estimated size", justify="right")
    table.add_column("Class")
    table.add_column("Audio")
    for job in manifest.jobs:
        size = (
            f"{job.estimated_bytes / (1024**2):.1f} MiB"
            if job.estimated_bytes is not None
            else "unknown"
        )
        table.add_row(
            job.job_id,
            str(job.title_index),
            size,
            job.classification,
            job.audio_policy,
        )
    console.print(table)
    console.print(
        f"[green]Bound manifest saved without execution authority: "
        f"{manifest_out}[/green]"
    )


@app.command("probe-mkv")
def probe_mkv(
    media_paths: Annotated[
        list[Path],
        typer.Argument(
            help="Explicit MKV file(s) to inspect sequentially with FFprobe",
        ),
    ],
    output_dir: Annotated[
        Path,
        typer.Option(
            "--output-dir",
            "-o",
            help="Directory for sanitized, replayable FFprobe JSON reports",
        ),
    ] = Path(".mkv-preflight/ffprobe"),
    ffprobe_path: Annotated[
        Path | None,
        typer.Option(
            "--ffprobe-path",
            help="Path to ffprobe.exe (otherwise use FFPROBE_PATH or PATH)",
        ),
    ] = None,
    timeout: Annotated[
        int,
        typer.Option(
            "--timeout",
            min=1,
            help="Maximum seconds allowed for each MKV inspection",
        ),
    ] = 60,
):
    """Read metadata from explicit MKVs without modifying media."""

    from mkv_episode_matcher.media.ffprobe_runner import (
        FFprobeError,
        inspect_mkv,
        resolve_ffprobe_path,
        write_sanitized_probe_report,
    )

    console.print(
        Panel(
            "READ-ONLY MKV INSPECTION\n"
            "Runs FFprobe metadata inspection on explicit MKV files only.\n"
            "No extraction, transcription, rename, move, delete, or transcode."
        )
    )

    try:
        executable = resolve_ffprobe_path(ffprobe_path)
        reports: list[tuple[str, object, Path]] = []
        for index, media_path in enumerate(media_paths, start=1):
            media_id = f"media-{index}"
            console.print(f"[blue]Inspecting {media_id}...[/blue]")
            inspection = inspect_mkv(
                executable,
                media_path,
                timeout_seconds=timeout,
            )
            report_path = write_sanitized_probe_report(
                output_dir,
                inspection.media,
                media_id=media_id,
            )
            reports.append((media_id, inspection.media, report_path))
    except FFprobeError as exc:
        console.print(f"[red]FFprobe inspection stopped safely: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    table = Table(title="Sanitized FFprobe reports")
    table.add_column("Media ID")
    table.add_column("Runtime", justify="right")
    table.add_column("Audio streams", justify="right")
    table.add_column("Report")
    for media_id, media, report_path in reports:
        duration = getattr(media, "duration_seconds")
        audio_streams = getattr(media, "audio_streams")
        table.add_row(
            media_id,
            f"{duration / 60:.1f} min",
            str(len(audio_streams)),
            str(report_path),
        )
    console.print(table)


@app.command("diagnose-transcript")
def diagnose_transcript_command(
    media_path: Annotated[
        Path,
        typer.Argument(
            help="One explicit MKV to sample without modifying it",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    start: Annotated[
        float,
        typer.Option("--start", min=0, help="Sample start time in seconds"),
    ] = 300.0,
    duration: Annotated[
        float,
        typer.Option(
            "--duration",
            min=5,
            max=60,
            help="Sample duration in seconds",
        ),
    ] = 30.0,
    media_id: Annotated[
        str,
        typer.Option(
            "--media-id",
            help="Redacted identifier stored in the metrics report",
        ),
    ] = "media-1",
    audio_stream: Annotated[
        int | None,
        typer.Option(
            "--audio-stream",
            min=0,
            help="Absolute FFprobe stream index to extract; defaults to FFmpeg selection",
        ),
    ] = None,
    report_out: Annotated[
        Path | None,
        typer.Option(
            "--report-out",
            help="New JSON path for redacted metrics (never transcript text)",
        ),
    ] = None,
):
    """Show a short Whisper excerpt and save dialogue-free diagnostic metrics."""

    from datetime import datetime

    from mkv_episode_matcher.core.providers.asr import get_asr_provider
    from mkv_episode_matcher.media.transcript_diagnostic import (
        TranscriptDiagnosticError,
        diagnose_transcript,
        write_safe_report,
    )

    console.print(
        Panel(
            "READ-ONLY TRANSCRIPT DIAGNOSTIC\n"
            "Extracts one temporary mono WAV, shows a short Whisper excerpt, "
            "then removes the WAV.\n"
            "No rename, move, delete, or transcode."
        )
    )
    try:
        config = get_config_manager().load()
        provider = get_asr_provider(
            model_type=config.asr_provider,
            model_name=config.asr_model_name,
        )
        diagnostic = diagnose_transcript(
            media_path,
            provider,
            media_id=media_id,
            start_seconds=start,
            duration_seconds=duration,
            model_name=config.asr_model_name,
            audio_stream_index=audio_stream,
        )
        if report_out is None:
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            report_out = (
                Path(".mkv-runs") / "transcript-diagnostics" / f"{timestamp}.json"
            )
        write_safe_report(report_out, diagnostic)
    except TranscriptDiagnosticError as exc:
        console.print(f"[red]Diagnostic stopped safely: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    table = Table(title=f"Transcript diagnostic: {media_id}")
    table.add_column("Start")
    table.add_column("Duration")
    table.add_column("Audio")
    table.add_column("Words")
    table.add_column("Mean")
    table.add_column("Peak")
    table.add_row(
        f"{diagnostic.start_seconds:.1f}s",
        f"{diagnostic.duration_seconds:.1f}s",
        (
            str(diagnostic.audio_stream_index)
            if diagnostic.audio_stream_index is not None
            else "default"
        ),
        str(diagnostic.transcript_words),
        (
            f"{diagnostic.mean_dbfs:.1f} dBFS"
            if diagnostic.mean_dbfs is not None
            else "silence"
        ),
        (
            f"{diagnostic.peak_dbfs:.1f} dBFS"
            if diagnostic.peak_dbfs is not None
            else "silence"
        ),
    )
    console.print(table)
    console.print("[bold]Short Whisper excerpt:[/bold]")
    console.print(diagnostic.excerpt or "[yellow]<no usable speech>[/yellow]")
    console.print(f"[green]Redacted metrics saved:[/green] {report_out}")


@app.command("collect-transcripts")
def collect_transcripts(
    media_paths: Annotated[
        list[Path],
        typer.Argument(
            help="Explicit MKV files to sample sequentially",
        ),
    ],
    media_ids: Annotated[
        list[str] | None,
        typer.Option(
            "--media-id",
            help="Repeat once per MKV, in the same order",
        ),
    ] = None,
    probe_reports: Annotated[
        list[Path] | None,
        typer.Option(
            "--probe-report",
            help="Repeat once per MKV with its saved sanitized FFprobe JSON",
        ),
    ] = None,
    report_out: Annotated[
        Path,
        typer.Option(
            "--report-out",
            help="New private JSON report containing transcript windows",
        ),
    ] = Path("transcripts.private.json"),
    metrics_out: Annotated[
        Path,
        typer.Option(
            "--metrics-out",
            help="New path- and dialogue-free JSON metrics report",
        ),
    ] = Path("transcripts.metrics.json"),
    ffmpeg_path: Annotated[
        Path | None,
        typer.Option(
            "--ffmpeg-path",
            help="Path to ffmpeg.exe (otherwise use FFMPEG_PATH or PATH)",
        ),
    ] = None,
    minimum_words: Annotated[
        int,
        typer.Option(
            "--minimum-words",
            min=1,
            max=200,
            help="Minimum total words required to accept an audio stream",
        ),
    ] = 8,
    maximum_streams: Annotated[
        int,
        typer.Option(
            "--maximum-streams",
            min=1,
            max=3,
            help="Maximum ranked audio streams attempted per file",
        ),
    ] = 3,
    sampling_mode: Annotated[
        str,
        typer.Option(
            "--sampling-mode",
            help="standard uses 25/50/75%; intro samples one reviewed start time",
        ),
    ] = "standard",
    intro_start_seconds: Annotated[
        float,
        typer.Option(
            "--intro-start",
            min=0,
            help="Start time in seconds for intro mode (clamped to file duration)",
        ),
    ] = 60.0,
    preferred_audio_stream: Annotated[
        int | None,
        typer.Option(
            "--preferred-audio-stream",
            min=0,
            help="Try this saved FFprobe stream index first",
        ),
    ] = None,
    confirm_read: Annotated[
        bool,
        typer.Option(
            "--confirm-read",
            help="Confirm reading the exact MKVs and extracting temporary audio",
        ),
    ] = False,
):
    """Collect private CPU Whisper windows from explicitly approved MKVs."""

    from mkv_episode_matcher.core.providers.asr import get_asr_provider
    from mkv_episode_matcher.media.probe import ProbeDataError, load_ffprobe_payload
    from mkv_episode_matcher.media.transcript_batch import (
        FFmpegSampleExtractor,
        TranscriptBatchError,
        TranscriptBatchItem,
        collect_transcript_batch,
        resolve_ffmpeg_path,
        validate_new_report_paths,
        write_private_transcript_report,
        write_safe_metrics_report,
    )

    console.print(
        Panel(
            "EXPLICIT READ-ONLY MEDIA ACCESS\n"
            "Reads only the listed MKVs, sequentially, and creates temporary WAV "
            "samples that are removed after the batch.\n"
            "Uses one CPU ASR model and saved FFprobe metadata. No media discovery, "
            "rename, move, delete, transcode, provider request, or Gemini call."
        )
    )
    media_ids = media_ids or []
    probe_reports = probe_reports or []
    if not (len(media_paths) == len(media_ids) == len(probe_reports) and media_paths):
        console.print(
            "[red]Provide exactly one --media-id and --probe-report per MKV.[/red]"
        )
        raise typer.Exit(code=1)
    if not confirm_read:
        console.print(
            "[yellow]Planning stopped before media access. "
            "Review the exact inputs and repeat with --confirm-read.[/yellow]"
        )
        raise typer.Exit(code=2)

    try:
        validate_new_report_paths(report_out, metrics_out)
        items = tuple(
            TranscriptBatchItem(
                file_id=media_id,
                media_path=media_path,
                media=load_ffprobe_payload(probe_report),
            )
            for media_path, media_id, probe_report in zip(
                media_paths,
                media_ids,
                probe_reports,
                strict=True,
            )
        )
        config = get_config_manager().load()
        provider = get_asr_provider(
            model_type=config.asr_provider,
            model_name=config.asr_model_name,
            device="cpu",
        )
        result = collect_transcript_batch(
            items,
            provider,
            FFmpegSampleExtractor(resolve_ffmpeg_path(ffmpeg_path)),
            model_name=config.asr_model_name,
            minimum_words=minimum_words,
            maximum_streams=maximum_streams,
            sampling_mode=sampling_mode,
            intro_start_seconds=intro_start_seconds,
            preferred_stream_index=preferred_audio_stream,
        )
        write_private_transcript_report(report_out, result)
        write_safe_metrics_report(metrics_out, result)
    except (TranscriptBatchError, ProbeDataError) as exc:
        console.print(f"[red]Transcript batch stopped safely: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    table = Table(title="CPU transcript batch")
    table.add_column("Media ID")
    table.add_column("Status")
    table.add_column("Audio stream", justify="right")
    table.add_column("Words", justify="right")
    for item in result.files:
        table.add_row(
            item.file_id,
            item.status,
            (
                str(item.audio_stream_index)
                if item.audio_stream_index is not None
                else "none"
            ),
            str(sum(window.word_count for window in item.windows)),
        )
    console.print(table)
    console.print(f"[green]Private transcript report saved:[/green] {report_out}")
    console.print(f"[green]Dialogue-free metrics saved:[/green] {metrics_out}")
    if not result.succeeded:
        console.print(
            "[yellow]One or more files require audio review; "
            "bundle generation will refuse them.[/yellow]"
        )
        raise typer.Exit(code=1)


@app.command("merge-transcript-reports")
def merge_transcript_reports(
    reports: Annotated[
        list[Path],
        typer.Argument(
            help="Explicit private saved-transcript reports to merge",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    report_out: Annotated[
        Path,
        typer.Option(
            "--report-out",
            help="New private merged transcript report",
        ),
    ],
    file_id_prefix: Annotated[
        str | None,
        typer.Option(
            "--file-id-prefix",
            help="Keep only redacted file IDs beginning with this prefix",
        ),
    ] = None,
    enrich_duplicates: Annotated[
        bool,
        typer.Option(
            "--enrich-duplicates",
            help="Merge additional windows for duplicate redacted file IDs",
        ),
    ] = False,
    skip_review_files: Annotated[
        bool,
        typer.Option(
            "--skip-review-files",
            help="Exclude review-audio rows while preserving them in source reports",
        ),
    ] = False,
    file_id_mappings: Annotated[
        list[str] | None,
        typer.Option(
            "--map-file-id",
            help="Map one saved redacted ID as SOURCE=TARGET; repeat as needed",
        ),
    ] = None,
):
    """Merge explicit private transcript reports without media or API access."""

    from mkv_episode_matcher.media.evidence_bundle import (
        EvidenceBundleError,
        merge_saved_transcript_evidence,
        write_merged_transcript_evidence,
    )

    console.print(
        Panel(
            "PRIVATE SAVED DATA ONLY\n"
            "Merges only the explicitly listed transcript reports.\n"
            "No MKV, Whisper, FFmpeg, provider, rename, move, or transcode access."
        )
    )
    try:
        file_id_map: dict[str, str] = {}
        for mapping in file_id_mappings or []:
            source_id, separator, target_id = mapping.partition("=")
            if not separator or not source_id or not target_id:
                raise EvidenceBundleError("File-ID mappings must use SOURCE=TARGET")
            if source_id in file_id_map:
                raise EvidenceBundleError("Each source file ID may be mapped only once")
            file_id_map[source_id] = target_id
        files = merge_saved_transcript_evidence(
            tuple(reports),
            file_id_prefix=file_id_prefix,
            enrich_duplicates=enrich_duplicates,
            skip_review_files=skip_review_files,
            file_id_map=file_id_map,
        )
        write_merged_transcript_evidence(report_out, files)
    except EvidenceBundleError as exc:
        console.print(f"[red]Transcript merge stopped safely: {exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Merged {len(files)} redacted file IDs.[/green]")
    console.print(f"[green]Private merged report saved:[/green] {report_out}")


@app.command("build-unmatched-bundle")
def build_unmatched_bundle(
    transcript_report: Annotated[
        Path,
        typer.Argument(
            help="Explicit saved multi-window transcript evidence JSON",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    catalog_report: Annotated[
        Path,
        typer.Argument(
            help="Explicit authoritative episode catalogue JSON",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    bundle_out: Annotated[
        Path,
        typer.Option(
            "--bundle-out",
            help="New private transient JSON bundle containing short excerpts",
        ),
    ],
    report_out: Annotated[
        Path,
        typer.Option(
            "--report-out",
            help="New dialogue-free local-ranking report",
        ),
    ],
    top_k: Annotated[
        int,
        typer.Option(
            "--top-k",
            min=1,
            max=50,
            help="Local candidate rows retained per file in the safe report",
        ),
    ] = 10,
):
    """Build a Gemini-ready bundle from saved data without media or API access."""

    from mkv_episode_matcher.media.evidence_bundle import (
        EvidenceBundleError,
        build_transient_evidence_bundle,
        load_episode_catalog,
        load_saved_transcript_evidence,
        validate_new_output_paths,
        write_safe_evidence_plan,
        write_transient_bundle,
    )

    console.print(
        Panel(
            "SAVED DATA ONLY\n"
            "Reads only the two explicit JSON reports and ranks candidates locally.\n"
            "The transient bundle contains short dialogue excerpts: keep it private.\n"
            "No Whisper/TMDb/Gemini request, MKV access, rename, move, or transcode."
        )
    )
    try:
        validate_new_output_paths(bundle_out, report_out)
        files = load_saved_transcript_evidence(transcript_report)
        catalog = load_episode_catalog(catalog_report)
        bundle, plan = build_transient_evidence_bundle(
            files,
            catalog,
            top_k=top_k,
        )
        write_transient_bundle(bundle_out, bundle)
        try:
            write_safe_evidence_plan(report_out, plan)
        except EvidenceBundleError:
            # Leave the collision-safe transient bundle intact for diagnosis.
            raise
    except EvidenceBundleError as exc:
        console.print(f"[red]Evidence bundle stopped safely: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    table = Table(title="Local unmatched evidence plan")
    table.add_column("Files", justify="right")
    table.add_column("Catalogue", justify="right")
    table.add_column("Shortlist", justify="right")
    table.add_row(
        str(plan.file_count),
        str(plan.catalog_episode_count),
        str(plan.shortlisted_episode_count),
    )
    console.print(table)
    console.print(f"[green]Private transient bundle saved:[/green] {bundle_out}")
    console.print(f"[green]Dialogue-free local plan saved:[/green] {report_out}")


@app.command("fetch-aired-catalog")
def fetch_aired_catalog(
    show_id: Annotated[
        int,
        typer.Argument(
            min=1,
            help="Reviewed TMDb TV-series ID",
        ),
    ],
    report_out: Annotated[
        Path,
        typer.Option(
            "--report-out",
            help="New path-free aired-order episode catalogue JSON",
        ),
    ],
):
    """Fetch an authoritative TMDb catalogue without sending media evidence."""

    from mkv_episode_matcher.core.credentials import (
        ApiCredentialError,
        ApiServiceError,
    )
    from mkv_episode_matcher.media.episode_catalog import (
        EpisodeCatalogError,
        write_episode_catalog,
    )
    from mkv_episode_matcher.tmdb_client import fetch_aired_episode_catalog

    console.print(
        Panel(
            "METADATA REQUEST ONLY\n"
            f"Fetches aired-order episode metadata for reviewed TMDb show ID {show_id}.\n"
            "No transcript, media path, MKV access, Gemini request, rename, or move."
        )
    )
    try:
        catalog = fetch_aired_episode_catalog(show_id)
        write_episode_catalog(report_out, catalog)
    except (EpisodeCatalogError, ApiCredentialError, ApiServiceError) as exc:
        console.print(f"[red]Episode catalogue fetch stopped safely: {exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Saved {len(catalog)} aired episodes:[/green] {report_out}")


@app.command("plan-disc-sequences")
def plan_disc_sequences_command(
    transcript_report: Annotated[
        Path,
        typer.Argument(
            help="Explicit saved private transcript evidence JSON",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    catalog_report: Annotated[
        Path,
        typer.Argument(
            help="Explicit authoritative aired-order catalogue JSON",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    group_specs: Annotated[
        list[str] | None,
        typer.Option(
            "--group",
            help=("Chronological GROUP=FILE,FILE declaration; repeat for later groups"),
        ),
    ] = None,
    report_out: Annotated[
        Path,
        typer.Option(
            "--report-out",
            help="New dialogue- and path-free sequence plan JSON",
        ),
    ] = Path("disc-sequence-plan.safe.json"),
    automatic_score: Annotated[
        float,
        typer.Option(
            "--automatic-score",
            min=0,
            max=1,
            help="Minimum mean lexical/runtime score for a proposal",
        ),
    ] = 0.55,
    automatic_margin: Annotated[
        float,
        typer.Option(
            "--automatic-margin",
            min=0,
            max=1,
            help="Minimum best-versus-next sequence margin for a proposal",
        ),
    ] = 0.02,
):
    """Plan ordered contiguous disc sequences from saved evidence only."""

    from mkv_episode_matcher.media.evidence_bundle import (
        EvidenceBundleError,
        load_episode_catalog,
        load_saved_transcript_evidence,
    )
    from mkv_episode_matcher.media.sequence_matcher import (
        SequenceMatchError,
        parse_sequence_group_specs,
        plan_disc_sequences,
        write_safe_sequence_plan,
    )

    console.print(
        Panel(
            "SAVED DATA ONLY\n"
            "Uses explicit chronological disc groups, saved transcripts, and a "
            "saved aired-order catalogue.\n"
            "No MKV, Whisper, FFmpeg, TMDb, Gemini, rename, move, or transcode "
            "access."
        )
    )
    try:
        groups = parse_sequence_group_specs(tuple(group_specs or ()))
        files = load_saved_transcript_evidence(transcript_report)
        catalog = load_episode_catalog(catalog_report)
        plan = plan_disc_sequences(
            files,
            catalog,
            groups,
            automatic_score=automatic_score,
            automatic_margin=automatic_margin,
        )
        write_safe_sequence_plan(report_out, plan)
    except (EvidenceBundleError, SequenceMatchError) as exc:
        console.print(f"[red]Disc-sequence plan stopped safely: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    table = Table(title="Saved disc-sequence plan")
    table.add_column("Group")
    table.add_column("Episodes")
    table.add_column("Score", justify="right")
    table.add_column("Margin", justify="right")
    table.add_column("Disposition")
    for group in plan.groups:
        table.add_row(
            group.group_id,
            " â€“ ".join((
                group.items[0].proposed_episode,
                group.items[-1].proposed_episode,
            )),
            f"{group.score:.3f}",
            f"{group.local_margin:.3f}",
            group.disposition,
        )
    console.print(table)
    console.print(
        f"[bold]Global:[/bold] score={plan.score:.3f}, "
        f"margin={plan.global_margin:.3f}, disposition={plan.disposition}"
    )
    console.print(f"[green]Dialogue-free sequence plan saved:[/green] {report_out}")


@app.command("plan-tv-organization")
def plan_tv_organization_command(
    sequence_report: Annotated[
        Path,
        typer.Argument(
            help="Explicit saved proposed disc-sequence plan JSON",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    catalog_report: Annotated[
        Path,
        typer.Argument(
            help="Explicit authoritative episode catalogue JSON",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    library_root: Annotated[
        Path,
        typer.Option(
            "--library-root",
            help="Existing TV library root; only destination names are inspected",
            exists=True,
            file_okay=False,
            readable=True,
        ),
    ],
    series_name: Annotated[
        str,
        typer.Option(
            "--series-name",
            help="Canonical one-component series folder and filename prefix",
        ),
    ],
    report_out: Annotated[
        Path,
        typer.Option(
            "--report-out",
            help="New relative-path organization proposal JSON",
        ),
    ] = Path("tv-organization-plan.safe.json"),
):
    """Plan Jellyfin/Plex names and inspect destination-name conflicts only."""

    from mkv_episode_matcher.media.evidence_bundle import (
        EvidenceBundleError,
        load_episode_catalog,
    )
    from mkv_episode_matcher.media.organizer import (
        OrganizationPlanError,
        load_sequence_assignments,
        plan_tv_organization,
        write_safe_organization_plan,
    )

    console.print(
        Panel(
            "PLAN ONLY â€” DESTINATION NAMES ONLY\n"
            "Builds relative Jellyfin/Plex names and inspects direct season-folder "
            "entries for episode conflicts.\n"
            "No media-content read, rename, move, overwrite, delete, directory "
            "creation, or transcode."
        )
    )
    try:
        assignments = load_sequence_assignments(sequence_report)
        catalog = load_episode_catalog(catalog_report)
        plan = plan_tv_organization(
            assignments,
            catalog,
            library_root=library_root,
            series_name=series_name,
        )
        write_safe_organization_plan(report_out, plan)
    except (EvidenceBundleError, OrganizationPlanError) as exc:
        console.print(f"[red]TV organization plan stopped safely: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    table = Table(title="TV organization proposal")
    table.add_column("Media ID")
    table.add_column("Episode")
    table.add_column("Status")
    for item in plan.items:
        table.add_row(item.file_id, item.episode_id, item.status)
    console.print(table)
    console.print(
        f"[bold]Summary:[/bold] proposed={plan.proposed_count}, "
        f"review={plan.review_count}, "
        f"missing-directories={len(plan.missing_directories)}"
    )
    console.print(f"[green]Relative organization plan saved:[/green] {report_out}")


@app.command("execute-tv-organization")
def execute_tv_organization_command(
    organization_report: Annotated[
        Path,
        typer.Argument(
            help="Explicit conflict-free TV organization plan JSON",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    handbrake_manifest: Annotated[
        Path,
        typer.Argument(
            help="Explicit HandBrake batch manifest that provides media-id source names",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    handbrake_events: Annotated[
        Path,
        typer.Argument(
            help="Append-only HandBrake batch event log proving completed outputs",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    encoded_root: Annotated[
        Path,
        typer.Option(
            "--encoded-root",
            help="Existing root containing verified HandBrake output MKVs",
            exists=True,
            file_okay=False,
            readable=True,
        ),
    ] = Path("."),
    destination_root: Annotated[
        Path,
        typer.Option(
            "--destination-root",
            help="Existing TV library root",
            exists=True,
            file_okay=False,
            readable=True,
        ),
    ] = Path("."),
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print planned moves without changing files"),
    ] = False,
    copy_only: Annotated[
        bool,
        typer.Option(
            "--copy",
            help="Copy instead of moving files into the TV library",
        ),
    ] = False,
    confirm_move: Annotated[
        bool,
        typer.Option(
            "--confirm-move",
            help="Required to apply moves; without this the command exits without changes",
        ),
    ] = False,
):
    """Apply a conflict-free TV organization plan into the library."""

    import shutil

    from mkv_episode_matcher.media.handbrake_batch import load_organization_targets
    from mkv_episode_matcher.media.handbrake_batch_executor import (
        HandBrakeBatchExecutionError,
        load_handbrake_batch_manifest,
        load_verified_encoded_outputs,
    )
    from mkv_episode_matcher.media.organizer import OrganizationPlanError

    console.print(
        Panel(
            "PLAN-APPLIED STAGE â€” ORGANIZATION\n"
            "Moves or copies already-transcoded MKVs from staging into the TV library "
            "after exact-match checks.\n"
            "No rip, transcode, delete, or overwrite action is performed."
        )
    )

    try:
        organization_targets = load_organization_targets(organization_report)
        loaded_manifest = load_handbrake_batch_manifest(handbrake_manifest)
        source_map = load_verified_encoded_outputs(
            loaded_manifest,
            encoded_root,
            handbrake_events,
        )

        organization_ids = {item.media_id for item in organization_targets}
        if set(source_map) != organization_ids:
            raise OrganizationPlanError(
                "Organization and HandBrake manifests do not have matching IDs"
            )

        destination_root = destination_root.resolve()
        proposed: list[tuple[str, Path, Path]] = []
        for target in organization_targets:
            source = source_map[target.media_id]
            if not source.exists():
                raise OrganizationPlanError(f"Source MKV is missing: {source.name}")
            if source.suffix.lower() != ".mkv":
                raise OrganizationPlanError("Source files must be MKV files")

            destination = (destination_root / target.relative_destination).resolve()
            try:
                destination.relative_to(destination_root)
            except ValueError as exc:
                raise OrganizationPlanError(
                    "Destination escapes the TV library root"
                ) from exc
            if destination.exists():
                raise OrganizationPlanError(
                    f"Destination already exists and would overwrite: {destination.name}"
                )
            if source == destination:
                raise OrganizationPlanError(
                    "Source and destination cannot be the same file"
                )
            proposed.append((target.media_id, source, destination))

    except (
        json.JSONDecodeError,
        OSError,
        HandBrakeBatchExecutionError,
        OrganizationPlanError,
    ) as exc:
        console.print(f"[red]Organization apply stopped safely: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    table = Table(title="TV organization apply preview")
    table.add_column("Media ID")
    table.add_column("Source")
    table.add_column("Destination")
    for media_id, source, destination in proposed:
        table.add_row(media_id, str(source), str(destination))
    console.print(table)

    if dry_run:
        console.print("[yellow]Dry-run mode: no files were changed.[/yellow]")
        return
    if not confirm_move:
        console.print(
            "[yellow]No changes were made; add --confirm-move to apply these moves.[/yellow]"
        )
        return

    moved_count = 0
    failures: list[str] = []
    for media_id, source, destination in proposed:
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if copy_only:
                shutil.copy2(source, destination)
            else:
                shutil.move(str(source), destination)
            moved_count += 1
            console.print(
                f"[green]{'Copied' if copy_only else 'Moved'}[/green] {media_id} "
                f"-> {destination.name}"
            )
        except OSError as exc:
            failures.append(f"{media_id}: {type(exc).__name__}")
            console.print(
                f"[red]Failed to process {media_id}: {type(exc).__name__}[/red]"
            )

    if failures:
        console.print(f"[red]Completed with {len(failures)} failure(s).[/red]")
        for failure in failures:
            console.print(f"[red]â€¢ {failure}[/red]")
        raise typer.Exit(code=2)

    console.print(
        f"[green]{'Copy' if copy_only else 'Move'} operation completed: "
        f"{moved_count}/{len(proposed)} file(s).[/green]"
    )


@app.command("plan-gemini-unmatched")
def plan_gemini_unmatched(
    bundle: Annotated[
        Path,
        typer.Argument(
            help=(
                "Transient JSON containing redacted file IDs, short transcript "
                "excerpts, and an authoritative episode catalogue"
            ),
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    report_out: Annotated[
        Path,
        typer.Option(
            "--report-out",
            help="New dialogue-free JSON request-plan path",
        ),
    ],
    model: Annotated[
        str,
        typer.Option(
            "--model",
            help="Gemini model name to place in the dry-run request plan",
        ),
    ] = "gemini-3.6-flash",
):
    """Validate and preview a Gemini fallback request without calling Gemini."""

    from mkv_episode_matcher.media.gemini_matcher import (
        GeminiMatchError,
        load_gemini_bundle,
        plan_gemini_request,
        write_safe_request_plan,
    )

    console.print(
        Panel(
            "PLAN ONLY\n"
            "Validates redacted evidence and allowed episode IDs.\n"
            "No Gemini/TMDb request, media access, rename, move, or transcode."
        )
    )
    try:
        files, catalog = load_gemini_bundle(bundle)
        plan = plan_gemini_request(model, files, catalog)
        write_safe_request_plan(report_out, plan)
    except GeminiMatchError as exc:
        console.print(f"[red]Gemini request planning stopped safely: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    table = Table(title="Gemini unmatched request plan")
    table.add_column("Files", justify="right")
    table.add_column("Candidates", justify="right")
    table.add_column("Model")
    table.add_row(
        str(len(plan.file_ids)),
        str(len(plan.candidate_episode_ids)),
        plan.model,
    )
    console.print(table)
    console.print(f"[green]Dialogue-free request plan saved:[/green] {report_out}")


@app.command("plan-rip")
def plan_rip(
    reports: Annotated[
        list[Path],
        typer.Argument(
            help="Fresh saved preflight inventory JSON file(s) to plan",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    manifest_out: Annotated[
        Path,
        typer.Option(
            "--manifest-out",
            help="New path for the redacted, execution-ready manifest",
        ),
    ],
    media_context: Annotated[
        Path,
        typer.Option(
            "--media-context",
            help=(
                "Reviewed JSON mapping disc IDs to series, season, and "
                "disc/volume metadata"
            ),
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
):
    """Build a path-redacted rip manifest without reading discs or media."""

    from mkv_episode_matcher.disc.rip_manifest import (
        build_rip_manifest,
        load_media_contexts,
        write_rip_manifest,
    )
    from mkv_episode_matcher.disc.ripper import RipError

    try:
        contexts = load_media_contexts(media_context)
        manifest = build_rip_manifest(reports, contexts)
        write_rip_manifest(manifest_out, manifest)
    except RipError as exc:
        console.print(f"[red]Rip planning stopped safely: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    table = Table(title="Rip plan (no disc or media access)")
    table.add_column("Job")
    table.add_column("Drive", justify="right")
    table.add_column("Title", justify="right")
    table.add_column("Estimated size", justify="right")
    for job in manifest.jobs:
        estimated = (
            f"{job.estimated_bytes / (1024**3):.2f} GiB"
            if job.estimated_bytes
            else "unknown"
        )
        table.add_row(
            job.job_id,
            str(job.drive_index),
            str(job.title_index),
            estimated,
        )
    console.print(table)
    for proof in manifest.disc_proofs:
        strategy = (
            f"single-open candidate (--minlength={proof.minimum_length_seconds})"
            if proof.batch_eligible
            else "per-title fallback"
        )
        console.print(f"[dim]{proof.disc_id}: {strategy}[/dim]")
    for skipped in manifest.skipped_discs:
        console.print(
            f"[yellow]{skipped.disc_id} excluded:[/yellow] "
            + ", ".join(skipped.reasons)
        )
    console.print(f"[green]Redacted manifest saved: {manifest_out}[/green]")


@app.command("execute-rip")
def execute_rip(
    manifest_path: Annotated[
        Path,
        typer.Argument(
            help="Previously reviewed approved-rip-plan JSON",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    output_root: Annotated[
        Path,
        typer.Option(
            "--output-root",
            help="Existing staging root that will receive new per-title directories",
        ),
    ],
    confirm_rip: Annotated[
        bool,
        typer.Option(
            "--confirm-rip",
            help="Required acknowledgement that this command reads discs and writes MKVs",
        ),
    ] = False,
    makemkv_path: Annotated[
        Path | None,
        typer.Option(
            "--makemkv-path",
            help="Path to makemkvcon64.exe",
        ),
    ] = None,
    timeout: Annotated[
        int,
        typer.Option(
            "--timeout",
            min=60,
            help="Maximum seconds allowed for each title",
        ),
    ] = 7200,
    log_dir: Annotated[
        Path,
        typer.Option(
            "--log-dir",
            help="Directory for append-only redacted JSONL run logs",
        ),
    ] = Path(".mkv-runs"),
    parallel_drives: Annotated[
        int | None,
        typer.Option(
            "--parallel-drives",
            min=1,
            help=(
                "Cap concurrent physical drives (default: all drives in the "
                "manifest); titles on each drive remain sequential"
            ),
        ),
    ] = None,
    sequential: Annotated[
        bool,
        typer.Option(
            "--sequential",
            help="Opt out of the default parallel-across-drives operation",
        ),
    ] = False,
    fresh_inventory: Annotated[
        list[Path] | None,
        typer.Option(
            "--fresh-inventory",
            help=(
                "Explicit fresh preflight report used to rebind an eligible "
                "single-open drive; repeat once per drive"
            ),
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ] = None,
):
    """Execute an approved title manifest with explicit safe concurrency."""

    from datetime import datetime

    from mkv_episode_matcher.disc.preflight import PreflightError, resolve_makemkv_path
    from mkv_episode_matcher.disc.rip_manifest import (
        bind_fresh_batch_plans,
        load_rip_manifest,
    )
    from mkv_episode_matcher.disc.rip_orchestrator import (
        run_auto_rip_queue,
        run_parallel_auto_rip_queue,
    )
    from mkv_episode_matcher.disc.ripper import (
        RipError,
        run_parallel_rip_queue,
        run_rip_queue,
    )

    if not confirm_rip:
        console.print(
            "[red]Execution refused. Review the manifest and pass --confirm-rip "
            "to authorize physical disc reads and MKV writes.[/red]"
        )
        raise typer.Exit(code=2)

    try:
        manifest = load_rip_manifest(manifest_path)
        batch_plans = bind_fresh_batch_plans(
            manifest,
            list(fresh_inventory or []),
        )
        executable = resolve_makemkv_path(makemkv_path)
        if not output_root.is_dir():
            raise RipError("Approved output root does not exist")

        log_dir.mkdir(parents=True, exist_ok=True)
        cancel_file = log_dir / "STOP"
        if cancel_file.exists():
            raise RipError(
                "Cancellation marker exists; remove it manually before a new run"
            )
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        log_path = log_dir / f"rip-{timestamp}.jsonl"
        if sequential and parallel_drives is not None:
            raise RipError("--sequential and --parallel-drives cannot be used together")
        base_operation = (
            "sequential operation"
            if sequential
            else (
                f"parallel across up to {parallel_drives} physical drive(s)"
                if parallel_drives is not None
                else "parallel across all physical drives in the manifest"
            )
        )
        operation = (
            f"{base_operation}; {len(batch_plans)} drive(s) use single-open "
            "and remaining drives use per-title fallback"
            if fresh_inventory
            else f"{base_operation}; per-title execution"
        )

        console.print(
            Panel(
                "AUTHORIZED RIP EXECUTION\n"
                f"{len(manifest.jobs)} title(s), {operation}\n"
                "New per-title staging directories only; no overwrite or ejection\n"
                f"Stop marker: {cancel_file}"
            )
        )

        def show_event(level: str, message: str) -> None:
            color = {
                "fatal": "red",
                "warning": "yellow",
                "progress": "blue",
            }.get(level, "white")
            console.print(f"[{color}]{message}[/{color}]")

        if not sequential and batch_plans:
            log_path = log_dir / f"parallel-{timestamp}"
            results = run_parallel_auto_rip_queue(
                executable,
                output_root,
                manifest.jobs,
                log_path,
                batch_plans=batch_plans,
                timeout_seconds=timeout,
                cancel_file=cancel_file,
                max_drives=parallel_drives,
                on_event=show_event,
            )
        elif not sequential:
            log_path = log_dir / f"parallel-{timestamp}"
            results = run_parallel_rip_queue(
                executable,
                output_root,
                manifest.jobs,
                log_path,
                timeout_seconds=timeout,
                cancel_file=cancel_file,
                max_drives=parallel_drives,
                on_event=show_event,
            )
        elif batch_plans:
            results = run_auto_rip_queue(
                executable,
                output_root,
                manifest.jobs,
                log_path,
                batch_plans=batch_plans,
                timeout_seconds=timeout,
                cancel_file=cancel_file,
                on_event=show_event,
            )
        else:
            results = run_rip_queue(
                executable,
                output_root,
                manifest.jobs,
                log_path,
                timeout_seconds=timeout,
                cancel_file=cancel_file,
                on_event=show_event,
            )
    except (RipError, PreflightError) as exc:
        console.print(f"[red]Rip queue stopped safely: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    total_bytes = sum(result.output_bytes for result in results)
    console.print(
        f"[green]Completed {len(results)} title(s), "
        f"{total_bytes / (1024**3):.2f} GiB verified.[/green]"
    )
    console.print(f"[dim]Redacted run log: {log_path}[/dim]")


@app.command("plan-special-feature-resume")
def plan_special_feature_resume(
    bound_manifest: Annotated[
        Path,
        typer.Argument(
            help="Original immutable bound special-feature manifest",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    original_inventory: Annotated[
        Path,
        typer.Option(
            "--original-inventory",
            help="Saved inventory used to validate the original binding",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    fresh_inventory: Annotated[
        Path,
        typer.Option(
            "--fresh-inventory",
            help="Fresh metadata-identical inventory for the relocated disc",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    events: Annotated[
        Path,
        typer.Option(
            "--events",
            help="Append-only event log from the interrupted run",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    bound_sha256: Annotated[
        str,
        typer.Option(
            "--bound-sha256",
            help="Exact SHA-256 of the original reviewed bound manifest",
        ),
    ],
    manifest_out: Annotated[
        Path,
        typer.Option(
            "--manifest-out",
            help="New path for the still-unauthorized resume manifest",
        ),
    ],
):
    """Plan unfinished special-feature jobs from saved run data only."""

    from mkv_episode_matcher.disc.special_feature_binder import (
        SpecialFeatureBindError,
        write_bound_special_feature_manifest,
    )
    from mkv_episode_matcher.disc.special_feature_resume import (
        build_special_feature_resume_manifest,
    )

    try:
        output = manifest_out.resolve(strict=False)
        inputs = {
            bound_manifest.resolve(strict=True),
            original_inventory.resolve(strict=True),
            fresh_inventory.resolve(strict=True),
            events.resolve(strict=True),
        }
        if output in inputs:
            raise SpecialFeatureBindError(
                "Resume manifest must be distinct from every input"
            )
        manifest = build_special_feature_resume_manifest(
            bound_manifest,
            original_inventory,
            fresh_inventory,
            events,
            expected_bound_sha256=bound_sha256,
        )
        write_bound_special_feature_manifest(manifest_out, manifest)
    except SpecialFeatureBindError as exc:
        console.print(f"[red]Resume planning stopped safely: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    console.print(
        Panel(
            "SPECIAL-FEATURE RESUME PLAN ONLY\n"
            "Completed and partial outputs remain untouched.\n"
            "No disc or media access occurred; separate authorization is required."
        )
    )
    table = Table(title="Unfinished titles rebound to collision-safe staging")
    table.add_column("Job")
    table.add_column("Title", justify="right")
    table.add_column("Drive", justify="right")
    table.add_column("Estimated size", justify="right")
    for job in manifest.jobs:
        size = (
            f"{job.estimated_bytes / (1024**2):.1f} MiB"
            if job.estimated_bytes is not None
            else "unknown"
        )
        table.add_row(job.job_id, str(job.title_index), str(job.drive_index), size)
    console.print(table)
    console.print(f"[green]Unauthorized resume manifest saved: {manifest_out}[/green]")


@app.command("execute-special-feature-rip")
def execute_special_feature_rip(
    bound_manifest: Annotated[
        Path,
        typer.Argument(
            help="Reviewed special-feature-rip-binding-plan JSON",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    fresh_inventory: Annotated[
        Path,
        typer.Option(
            "--fresh-inventory",
            help="Fresh saved preflight inventory used for final revalidation",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    bound_sha256: Annotated[
        str,
        typer.Option(
            "--bound-sha256",
            help="Exact explicitly authorized SHA-256 of the bound manifest",
        ),
    ],
    authorized_job_count: Annotated[
        int,
        typer.Option(
            "--authorized-job-count",
            min=1,
            help="Exact explicitly authorized number of titles",
        ),
    ],
    output_root: Annotated[
        Path,
        typer.Option(
            "--output-root",
            help="Existing isolated staging root",
            exists=True,
            file_okay=False,
        ),
    ],
    run_dir: Annotated[
        Path,
        typer.Option(
            "--run-dir",
            help="New dedicated directory for redacted logs and STOP marker",
        ),
    ],
    confirm_special_feature_rip: Annotated[
        bool,
        typer.Option(
            "--confirm-special-feature-rip",
            help="Required acknowledgement of the exact authorized disc writes",
        ),
    ] = False,
    makemkv_path: Annotated[
        Path | None,
        typer.Option("--makemkv-path", help="Path to makemkvcon64.exe"),
    ] = None,
    timeout: Annotated[
        int,
        typer.Option(
            "--timeout",
            min=60,
            help="Maximum seconds allowed for each title",
        ),
    ] = 7200,
):
    """Execute one exact bound special-feature manifest sequentially."""

    from mkv_episode_matcher.disc.preflight import (
        PreflightError,
        resolve_makemkv_path,
    )
    from mkv_episode_matcher.disc.ripper import RipError
    from mkv_episode_matcher.disc.special_feature_binder import (
        SpecialFeatureBindError,
        load_bound_special_feature_manifest,
    )
    from mkv_episode_matcher.disc.special_feature_executor import (
        execute_bound_special_feature_manifest,
    )

    if not confirm_special_feature_rip:
        console.print(
            "[red]Execution refused. Exact bound-manifest authorization and "
            "--confirm-special-feature-rip are required.[/red]"
        )
        raise typer.Exit(code=2)

    try:
        manifest = load_bound_special_feature_manifest(
            bound_manifest,
            fresh_inventory,
            expected_bound_sha256=bound_sha256,
        )
        executable = resolve_makemkv_path(makemkv_path)

        console.print(
            Panel(
                "AUTHORIZED SPECIAL-FEATURE RIP\n"
                f"Manifest SHA-256: {bound_sha256}\n"
                f"Titles: {authorized_job_count}, sequential operation\n"
                "Collision-refusing isolated staging; no library move, "
                "transcode, deletion, or ejection."
            )
        )

        def show_event(level: str, message: str) -> None:
            color = {
                "fatal": "red",
                "warning": "yellow",
                "progress": "blue",
            }.get(level, "white")
            console.print(f"[{color}]{message}[/{color}]")

        results = execute_bound_special_feature_manifest(
            manifest,
            bound_manifest_sha256=bound_sha256,
            executable=executable,
            output_root=output_root,
            run_dir=run_dir,
            authorized_job_count=authorized_job_count,
            timeout_seconds=timeout,
            on_event=show_event,
        )
    except (SpecialFeatureBindError, RipError, PreflightError) as exc:
        console.print(f"[red]Special-feature rip stopped safely: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    total_bytes = sum(result.output_bytes for result in results)
    console.print(
        f"[green]Completed {len(results)} special-feature title(s), "
        f"{total_bytes / (1024**3):.2f} GiB verified.[/green]"
    )
    console.print(f"[dim]Redacted run directory: {run_dir}[/dim]")


@app.command("plan-handbrake-batch")
def plan_handbrake_batch_command(
    organization_report: Annotated[
        Path,
        typer.Argument(
            help="Explicit conflict-free TV organization plan JSON",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    source_specs: Annotated[
        list[str] | None,
        typer.Option(
            "--source",
            help="Exact MEDIA_ID=MKV mapping; repeat once per planned episode",
        ),
    ] = None,
    output_root: Annotated[
        Path,
        typer.Option(
            "--output-root",
            help="Existing root beneath which encoded staging is proposed",
            exists=True,
            file_okay=False,
            readable=True,
        ),
    ] = Path("."),
    staging_prefix: Annotated[
        str,
        typer.Option(
            "--staging-prefix",
            help="One new relative staging component beneath the output root",
        ),
    ] = "encoded-staging",
    manifest_out: Annotated[
        Path,
        typer.Option(
            "--manifest-out",
            help="New path-redacted HandBrake batch manifest",
        ),
    ] = Path("handbrake-batch.safe.json"),
    handbrake_path: Annotated[
        Path | None,
        typer.Option(
            "--handbrake-path",
            help="Path to HandBrakeCLI.exe (otherwise use configuration)",
        ),
    ] = None,
    encoder: Annotated[
        str,
        typer.Option(help="Explicit AMD VCN encoder"),
    ] = "vce_h265",
    quality: Annotated[
        float,
        typer.Option(min=0, max=51, help="HandBrake constant-quality value"),
    ] = 26.0,
    content_kind: Annotated[
        str,
        typer.Option(
            "--content-kind",
            help="Reviewed content kind: unknown, live_action, or animation",
        ),
    ] = "unknown",
    nlmeans: Annotated[
        str | None,
        typer.Option(
            "--nlmeans",
            help="Optional NLMeans preset: ultralight, light, medium, or strong",
        ),
    ] = None,
    nlmeans_tune: Annotated[
        str,
        typer.Option(
            "--nlmeans-tune",
            help="NLMeans content tune; requires --nlmeans",
        ),
    ] = "none",
    reserve_gib: Annotated[
        int,
        typer.Option(
            "--reserve-gib",
            min=0,
            max=1024,
            help="Free-space reserve retained beyond total source bytes",
        ),
    ] = 10,
):
    """Plan a path-redacted AMD VCN batch without creating or transcoding."""

    from mkv_episode_matcher.media.handbrake import (
        HandBrakeError,
        HandBrakeProfile,
        inspect_handbrake_capabilities,
        resolve_handbrake_path,
    )
    from mkv_episode_matcher.media.handbrake_batch import (
        HandBrakeBatchError,
        load_organization_targets,
        plan_handbrake_batch,
        write_handbrake_batch_manifest,
    )

    console.print(
        Panel(
            "PLAN ONLY â€” FILE METADATA AND CAPABILITY CHECK\n"
            "Validates exact MKV names, sizes, output collisions, free space, and "
            "AMD VCN availability.\n"
            "No media-content read, directory creation, HandBrake transcode, "
            "rename, move, overwrite, or delete."
        )
    )
    try:
        sources: dict[str, Path] = {}
        for spec in source_specs or []:
            media_id, separator, raw_path = spec.partition("=")
            if not separator or not media_id or not raw_path or media_id in sources:
                raise HandBrakeBatchError(
                    "Source mappings must use unique MEDIA_ID=MKV values"
                )
            sources[media_id] = Path(raw_path)
        executable = resolve_handbrake_path(handbrake_path)
        capabilities = inspect_handbrake_capabilities(executable)
        targets = load_organization_targets(organization_report)
        manifest = plan_handbrake_batch(
            targets,
            sources,
            output_root=output_root,
            staging_prefix=staging_prefix,
            profile=HandBrakeProfile(
                encoder=encoder,
                quality=quality,
                content_kind=content_kind,
                nlmeans_preset=nlmeans,
                nlmeans_tune=nlmeans_tune,
            ),
            capabilities=capabilities,
            reserve_bytes=reserve_gib * 1024**3,
        )
        write_handbrake_batch_manifest(manifest_out, manifest)
    except (HandBrakeError, HandBrakeBatchError) as exc:
        console.print(f"[red]HandBrake batch planning stopped safely: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    console.print(
        Panel(
            "BATCH MANIFEST READY â€” NO TRANSCODE\n"
            f"Jobs: {manifest.job_count}\n"
            f"Encoder: {manifest.profile['encoder']}\n"
            f"Source bytes: {manifest.total_source_bytes}\n"
            f"Required free bytes: {manifest.required_free_bytes}\n"
            f"Available free bytes: {manifest.available_free_bytes}\n"
            f"Missing staging directories: {len(manifest.missing_directories)}\n"
            f"Status: {manifest.status}"
        )
    )
    console.print(f"[green]Path-redacted batch manifest saved:[/green] {manifest_out}")


@app.command("plan-handbrake")
def plan_handbrake(
    source: Annotated[
        Path,
        typer.Argument(
            help="One explicit source MKV",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    destination: Annotated[
        Path,
        typer.Argument(help="New MKV path in a separate existing staging directory"),
    ],
    media_id: Annotated[
        str,
        typer.Option("--media-id", help="Path-free identifier used in logs"),
    ],
    encoder: Annotated[
        str,
        typer.Option(help="Explicit AMD VCN encoder"),
    ] = "vce_h265",
    quality: Annotated[
        float,
        typer.Option(min=0, max=51, help="HandBrake constant-quality value"),
    ] = 26.0,
    content_kind: Annotated[
        str,
        typer.Option(
            "--content-kind",
            help="Reviewed content kind: unknown, live_action, or animation",
        ),
    ] = "unknown",
    nlmeans: Annotated[
        str | None,
        typer.Option(
            "--nlmeans",
            help="Optional NLMeans preset: ultralight, light, medium, or strong",
        ),
    ] = None,
    nlmeans_tune: Annotated[
        str,
        typer.Option(
            "--nlmeans-tune",
            help="NLMeans content tune; requires --nlmeans",
        ),
    ] = "none",
    sample_start: Annotated[
        int | None,
        typer.Option(min=0, help="Optional sample start in seconds"),
    ] = None,
    sample_duration: Annotated[
        int | None,
        typer.Option(min=10, max=600, help="Optional sample duration in seconds"),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit a path-free JSON plan"),
    ] = False,
):
    """Validate and display a HandBrake job without starting a transcode."""

    from mkv_episode_matcher.media.handbrake import (
        HandBrakeError,
        HandBrakeJob,
        HandBrakeProfile,
        validate_handbrake_job,
    )

    try:
        job = validate_handbrake_job(
            HandBrakeJob(
                media_id=media_id,
                source=source,
                destination=destination,
                profile=HandBrakeProfile(
                    encoder=encoder,
                    quality=quality,
                    content_kind=content_kind,
                    nlmeans_preset=nlmeans,
                    nlmeans_tune=nlmeans_tune,
                ),
                sample_start_seconds=sample_start,
                sample_duration_seconds=sample_duration,
            )
        )
    except HandBrakeError as exc:
        console.print(f"[red]HandBrake planning stopped safely: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    if json_output:
        typer.echo(json.dumps(job.safe_plan(), indent=2, sort_keys=True))
        return
    console.print(
        Panel(
            "PLAN ONLY â€” NO TRANSCODE\n"
            f"Media ID: {job.media_id}\n"
            f"Encoder: {job.profile.encoder} ({job.profile.encoder_preset})\n"
            f"Quality: {job.profile.quality}\n"
            f"Content: {job.profile.content_kind}\n"
            "Audio: AAC 256 kbps Pro Logic II default + source passthrough\n"
            f"NLMeans: {job.profile.nlmeans_preset or 'off'} "
            f"({job.profile.nlmeans_tune})\n"
            f"Sample: {job.sample_start_seconds}s / "
            f"{job.sample_duration_seconds}s"
        )
    )


@app.command("execute-handbrake")
def execute_handbrake(
    source: Annotated[
        Path,
        typer.Argument(
            help="One explicitly approved source MKV",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    destination: Annotated[
        Path,
        typer.Argument(help="New MKV path in a separate existing staging directory"),
    ],
    media_id: Annotated[
        str,
        typer.Option("--media-id", help="Path-free identifier used in logs"),
    ],
    confirm_transcode: Annotated[
        bool,
        typer.Option(
            "--confirm-transcode",
            help="Required acknowledgement that HandBrake will write media",
        ),
    ] = False,
    handbrake_path: Annotated[
        Path | None,
        typer.Option("--handbrake-path", help="Path to HandBrakeCLI.exe"),
    ] = None,
    ffprobe_path: Annotated[
        Path | None,
        typer.Option("--ffprobe-path", help="Path to ffprobe.exe"),
    ] = None,
    encoder: Annotated[
        str,
        typer.Option(help="Explicit AMD VCN encoder"),
    ] = "vce_h265",
    quality: Annotated[
        float,
        typer.Option(min=0, max=51, help="HandBrake constant-quality value"),
    ] = 26.0,
    content_kind: Annotated[
        str,
        typer.Option(
            "--content-kind",
            help="Reviewed content kind: unknown, live_action, or animation",
        ),
    ] = "unknown",
    nlmeans: Annotated[
        str | None,
        typer.Option(
            "--nlmeans",
            help="Optional NLMeans preset: ultralight, light, medium, or strong",
        ),
    ] = None,
    nlmeans_tune: Annotated[
        str,
        typer.Option(
            "--nlmeans-tune",
            help="NLMeans content tune; requires --nlmeans",
        ),
    ] = "none",
    sample_start: Annotated[
        int | None,
        typer.Option(min=0, help="Optional sample start in seconds"),
    ] = None,
    sample_duration: Annotated[
        int | None,
        typer.Option(min=10, max=600, help="Optional sample duration in seconds"),
    ] = None,
    timeout: Annotated[
        int,
        typer.Option(min=60, help="Maximum transcode time in seconds"),
    ] = 21600,
    run_dir: Annotated[
        Path,
        typer.Option(help="Directory for redacted process and event logs"),
    ] = Path(".mkv-runs"),
):
    """Execute one approved AMD VCN HandBrake job and verify its output."""

    from mkv_episode_matcher.media.ffprobe_runner import (
        FFprobeError,
        resolve_ffprobe_path,
    )
    from mkv_episode_matcher.media.handbrake import (
        HandBrakeError,
        HandBrakeJob,
        HandBrakeProfile,
        execute_handbrake_job,
        resolve_handbrake_path,
    )

    if not confirm_transcode:
        console.print(
            "[red]Execution refused. Review plan-handbrake output and pass "
            "--confirm-transcode to authorize the media write.[/red]"
        )
        raise typer.Exit(code=2)

    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        job = HandBrakeJob(
            media_id=media_id,
            source=source,
            destination=destination,
            profile=HandBrakeProfile(
                encoder=encoder,
                quality=quality,
                content_kind=content_kind,
                nlmeans_preset=nlmeans,
                nlmeans_tune=nlmeans_tune,
            ),
            sample_start_seconds=sample_start,
            sample_duration_seconds=sample_duration,
        )
        result = execute_handbrake_job(
            resolve_handbrake_path(handbrake_path),
            resolve_ffprobe_path(ffprobe_path),
            job,
            run_dir,
            confirm_transcode=True,
            timeout_seconds=timeout,
        )
    except (HandBrakeError, FFprobeError) as exc:
        console.print(f"[red]HandBrake execution stopped safely: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    console.print(
        Panel(
            "VERIFIED TRANSCODE COMPLETE\n"
            f"Media ID: {result.media_id}\n"
            f"Video: {result.video_codec} via {result.encoder}\n"
            f"Audio streams: {result.audio_streams}\n"
            f"Subtitle streams: {result.subtitle_streams}\n"
            f"Duration: {result.duration_seconds:.1f}s\n"
            f"Bytes: {result.output_bytes}"
        )
    )
    console.print(f"[dim]Redacted event log: {result.event_log}[/dim]")
    console.print(f"[dim]Redacted process log: {result.process_log}[/dim]")


@app.command("execute-handbrake-batch")
def execute_handbrake_batch_command(
    manifest: Annotated[
        Path,
        typer.Argument(
            help="One reviewed path-redacted HandBrake batch manifest",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    source_root: Annotated[
        Path,
        typer.Option(
            "--source-root",
            help="Existing directory containing exactly the planned source names",
            exists=True,
            file_okay=False,
            readable=True,
        ),
    ],
    output_root: Annotated[
        Path,
        typer.Option(
            "--output-root",
            help="Existing encoded-staging root; never a media-library root",
            exists=True,
            file_okay=False,
            writable=True,
        ),
    ],
    run_dir: Annotated[
        Path,
        typer.Option(
            "--run-dir",
            help="Dedicated directory for resumable path-redacted batch events",
        ),
    ],
    confirm_transcode: Annotated[
        bool,
        typer.Option(
            "--confirm-transcode",
            help="Required acknowledgement for this exact full batch",
        ),
    ] = False,
    handbrake_path: Annotated[
        Path | None,
        typer.Option("--handbrake-path", help="Path to HandBrakeCLI.exe"),
    ] = None,
    ffprobe_path: Annotated[
        Path | None,
        typer.Option("--ffprobe-path", help="Path to ffprobe.exe"),
    ] = None,
    max_workers: Annotated[
        int,
        typer.Option(
            "--max-workers",
            min=1,
            max=2,
            help="Bounded concurrent HandBrake jobs",
        ),
    ] = 2,
    max_jobs: Annotated[
        int,
        typer.Option(
            "--max-jobs",
            min=1,
            help="Maximum total jobs attempted in this invocation",
        ),
    ] = 2,
    timeout: Annotated[
        int,
        typer.Option(
            min=60,
            help="Maximum time for each individual transcode in seconds",
        ),
    ] = 21600,
):
    """Execute one confirmed immutable batch with pause/stop and safe resume."""

    from mkv_episode_matcher.media.ffprobe_runner import (
        FFprobeError,
        resolve_ffprobe_path,
    )
    from mkv_episode_matcher.media.handbrake import (
        HandBrakeError,
        resolve_handbrake_path,
    )
    from mkv_episode_matcher.media.handbrake_batch_executor import (
        HandBrakeBatchExecutionError,
        execute_handbrake_batch,
        load_handbrake_batch_manifest,
        validate_batch_run_dir_scope,
    )

    if not confirm_transcode:
        console.print(
            "[red]Batch execution refused. Present the exact immutable manifest "
            "and pass --confirm-transcode only after explicit approval.[/red]"
        )
        raise typer.Exit(code=2)

    try:
        if run_dir.exists() and not run_dir.is_dir():
            raise HandBrakeBatchExecutionError(
                "Batch run-log destination is not a directory"
            )
        validate_batch_run_dir_scope(run_dir, source_root, output_root)
        run_dir.mkdir(parents=True, exist_ok=True)
        result = execute_handbrake_batch(
            resolve_handbrake_path(handbrake_path),
            resolve_ffprobe_path(ffprobe_path),
            load_handbrake_batch_manifest(manifest),
            source_root,
            output_root,
            run_dir,
            confirm_transcode=True,
            max_workers=max_workers,
            max_jobs=max_jobs,
            timeout_seconds=timeout,
        )
    except (
        FFprobeError,
        HandBrakeError,
        HandBrakeBatchExecutionError,
    ) as exc:
        console.print(f"[red]HandBrake batch stopped safely: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    console.print(
        Panel(
            "HANDBRAKE BATCH RETURNED\n"
            f"Status: {result.status}\n"
            f"Manifest SHA-256: {result.manifest_sha256}\n"
            f"Jobs: {result.job_count}\n"
            f"Completed: {len(result.completed_ids)}\n"
            f"Resumed: {len(result.resumed_ids)}\n"
            f"Failed: {len(result.failed_ids)}\n"
            f"Blocked: {len(result.blocked_ids)}\n"
            f"Pending: {len(result.pending_ids)}"
        )
    )
    console.print(f"[green]Path-redacted batch events:[/green] {result.event_log}")


@app.command("plan-audio")
def plan_audio(
    probe_reports: Annotated[
        list[Path],
        typer.Argument(
            help="Saved FFprobe JSON file(s) to plan diagnostics for",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit machine-readable diagnostic plan JSON",
        ),
    ] = False,
):
    """Plan audio diagnostics from saved metadata without reading media."""

    from mkv_episode_matcher.media.audio_diagnostics import (
        build_audio_diagnostic_plan,
    )
    from mkv_episode_matcher.media.probe import (
        ProbeDataError,
        load_ffprobe_payload,
    )

    try:
        plans = [
            build_audio_diagnostic_plan(
                load_ffprobe_payload(report),
                media_id=f"media-{index}",
            )
            for index, report in enumerate(probe_reports, start=1)
        ]
    except ProbeDataError as exc:
        if json_output:
            typer.echo(
                json.dumps({
                    "mode": "plan-only",
                    "status": "error",
                    "error": str(exc),
                })
            )
        else:
            console.print(f"[red]Audio planning stopped safely: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "mode": "plan-only",
                    "plans": [plan.to_dict() for plan in plans],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return

    console.print(
        Panel(
            "PLAN ONLY\n"
            "Reads saved FFprobe JSON; does not run FFprobe or FFmpeg and does "
            "not read a media file.\n"
            "No extraction, transcription, rename, move, delete, or transcode."
        )
    )
    for plan in plans:
        table = Table(title=f"{plan.media_id}: audio diagnostic order")
        table.add_column("Rank", justify="right")
        table.add_column("Stream", justify="right")
        table.add_column("Role")
        table.add_column("Downmix")
        table.add_column("Reason")
        for stream in plan.streams:
            table.add_row(
                str(stream.rank),
                str(stream.stream_index),
                stream.role,
                stream.downmix,
                " ".join(stream.reasons),
            )
        console.print(table)
        windows = ", ".join(
            f"{window.start_seconds:.1f}s/{window.duration_seconds}s"
            for window in plan.sample_windows
        )
        console.print(f"[dim]Planned sample windows: {windows}[/dim]")
        console.print(f"[dim]{plan.fallback_policy}[/dim]")


@app.command()
def version():
    """Show version information."""
    try:
        import mkv_episode_matcher

        version = mkv_episode_matcher.__version__
    except AttributeError:
        version = "unknown"

    console.print(f"RipWeaver v{version}")


def _display_comprehensive_summary(results, failures, dry_run, output_dir, console):
    """Display a comprehensive summary of matching results."""
    from collections import defaultdict

    console.print("\n[bold green]Processing Complete![/bold green]")
    console.print(f"[blue]Successfully processed {len(results)} files[/blue]")
    if failures:
        console.print(f"[red]Failed to match {len(failures)} files[/red]\n")
    else:
        console.print("\n")

    # Group results by series/season for organized display
    series_groups = defaultdict(lambda: defaultdict(list))
    total_confidence = 0

    for result in results:
        series_name = result.episode_info.series_name
        season = result.episode_info.season
        series_groups[series_name][season].append(result)
        total_confidence += result.confidence

    # Create summary table
    table = Table(title="Episode Matching Summary")
    table.add_column("Original File", style="cyan")
    table.add_column("New Name", style="green")
    table.add_column("Episode", style="magenta", justify="center")
    table.add_column("Confidence", style="yellow", justify="center")
    table.add_column("Status", style="white", justify="center")

    for series_name in sorted(series_groups.keys()):
        for season in sorted(series_groups[series_name].keys()):
            episodes = series_groups[series_name][season]

            # Add series header if multiple series
            if len(series_groups) > 1:
                table.add_row(
                    f"[bold cyan]{series_name} - Season {season}[/bold cyan]",
                    "",
                    "",
                    "",
                    "",
                    style="bold cyan",
                )

            for result in sorted(episodes, key=lambda x: x.episode_info.episode):
                # Use original filename if available, otherwise current filename
                original_name = (
                    result.original_file.name
                    if result.original_file
                    else result.matched_file.name
                )

                # Generate expected new name
                title_part = (
                    f" - {result.episode_info.title}"
                    if result.episode_info.title
                    else ""
                )
                new_name = f"{result.episode_info.series_name} - {result.episode_info.s_e_format}{title_part}{result.matched_file.suffix}"

                # Clean the new name for display
                import re

                new_name = re.sub(r'[<>:"/\\\\|?*]', "", new_name).strip()

                status = (
                    "WOULD RENAME"
                    if dry_run
                    else (
                        "RENAMED"
                        if (
                            result.original_file
                            and result.original_file.name != result.matched_file.name
                        )
                        else "COPY"
                        if output_dir
                        else "RENAMED"
                    )
                )

                table.add_row(
                    original_name,
                    new_name,
                    result.episode_info.s_e_format,
                    f"{result.confidence:.2f}",
                    status,
                )

    # Add failures to table if any
    if failures:
        for failure in failures:
            table.add_row(
                failure.original_file.name,
                "-",
                "-",
                f"{failure.confidence:.2f}" if failure.confidence > 0 else "-",
                "[red]FAILED[/red]",
            )

    console.print(table)

    # Display summary statistics
    avg_confidence = total_confidence / len(results) if results else 0
    console.print("\n[bold]Summary Statistics:[/bold]")
    console.print(f"  Total episodes matched: [green]{len(results)}[/green]")
    if failures:
        console.print(f"  Total failures: [red]{len(failures)}[/red]")
    console.print(
        f"  Average confidence (matches): [yellow]{avg_confidence:.2f}[/yellow]"
    )
    console.print(f"  Series processed: [blue]{len(series_groups)}[/blue]")

    # Season breakdown
    season_count = sum(len(seasons) for seasons in series_groups.values())
    console.print(f"  Seasons processed: [magenta]{season_count}[/magenta]")

    # Display action taken
    console.print("\n[bold]Action Taken:[/bold]")
    if dry_run:
        console.print("[yellow]DRY RUN - No files were actually renamed[/yellow]")
        console.print(
            "Run the command without [bold]--dry-run[/bold] to perform the renames"
        )
    elif output_dir:
        console.print(f"[blue]Files copied to: {output_dir}[/blue]")
        console.print("Original files remain unchanged")
    else:
        console.print("[green]Files renamed in place[/green]")
        console.print("Original filenames have been updated")

    # Show command to view renamed files
    if not dry_run:
        if output_dir:
            console.print(f'\n[dim]View results: ls "{output_dir}"[/dim]')
        else:
            # Get the parent directory of the first result for the ls command
            if results:
                first_file_dir = results[0].matched_file.parent
                console.print(f'\n[dim]View results: ls "{first_file_dir}"[/dim]')

    # Warning for failures
    if failures:
        console.print("\n[bold red]Warnings:[/bold red]")
        console.print(
            f"[yellow]  â€¢ {len(failures)} files could not be matched.[/yellow]"
        )
        console.print("  â€¢ Try checking if correct subtitles are available online.")
        console.print(
            "  â€¢ Consider lowering the confidence threshold with [bold]--confidence[/bold] if matches are close."
        )


@app.command()
def serve(
    port: int = typer.Option(8001, "--port", "-p", help="Port to run the server on"),
    host: str = typer.Option("0.0.0.0", "--host", help="Host to bind to"),
    no_browser: bool = typer.Option(
        False, "--no-browser", help="Don't open browser automatically"
    ),
    hold_automatic_rips: bool = typer.Option(
        False,
        "--hold-automatic-rips",
        help=(
            "Discover and review loaded discs without launching unattended rip or "
            "downstream workers for this server lifetime"
        ),
    ),
):
    """
    Launch the Web UI server.

    Starts the backend API server and opens the web interface in your browser.
    This is the recommended way to use RipWeaver for most users.

    Examples:

        # Start web UI on default port
        mkv-match serve

        # Start on custom port without opening browser
        mkv-match serve --port 9000 --no-browser
    """
    import threading
    import time
    import webbrowser

    from mkv_episode_matcher.backend.automatic_rip import (
        set_automatic_rip_startup_hold,
    )

    set_automatic_rip_startup_hold(hold_automatic_rips)
    from mkv_episode_matcher.backend.main import run_uvicorn_server

    print_banner()
    console.print(f"[blue]Starting Web UI server on http://{host}:{port}[/blue]")
    if hold_automatic_rips:
        console.print(
            "[yellow]Automatic disc ripping and downstream processing are held for "
            "this server lifetime.[/yellow]"
        )
    console.print("[dim]Press Ctrl+C to stop the server[/dim]\n")

    if not no_browser:

        def open_browser():
            time.sleep(1.5)
            webbrowser.open(f"http://localhost:{port}")

        threading.Thread(target=open_browser, daemon=True).start()

    run_uvicorn_server(host=host, port=port)


@app.command()
def gui(
    port: int = typer.Option(8001, "--port", "-p", help="Port to run the server on"),
    no_browser: bool = typer.Option(
        False, "--no-browser", help="Don't open browser automatically"
    ),
):
    """Launch the Web UI (alias for 'serve')."""
    serve(
        port=port,
        host="0.0.0.0",
        no_browser=no_browser,
        hold_automatic_rips=False,
    )


if __name__ == "__main__":
    app()
