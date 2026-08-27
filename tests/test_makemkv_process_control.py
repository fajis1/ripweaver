import subprocess

import pytest

from mkv_episode_matcher.disc import makemkv_process_control
from mkv_episode_matcher.disc.makemkv_process_control import (
    ExclusiveMakeMKVStartupControl,
    MakeMKVCommandScope,
    MakeMKVHardwareArbiter,
    MakeMKVJobSupervisor,
    MakeMKVProcessControlError,
    ProcessSnapshotEntry,
    StartupCleanupResult,
    SupervisedShutdownResult,
    cleanup_existing_makemkv_processes,
    is_makemkv_cli_name,
    makemkv_command_scope,
)


@pytest.fixture(autouse=True)
def _isolated_hardware_arbiter(monkeypatch):
    monkeypatch.setattr(
        makemkv_process_control,
        "_MAKEMKV_HARDWARE_ARBITER",
        MakeMKVHardwareArbiter(),
    )


def test_cli_name_scope_is_exact_and_case_insensitive():
    assert is_makemkv_cli_name("MakeMKVCon64.EXE") is True
    assert is_makemkv_cli_name("makemkvcon.exe") is True
    assert is_makemkv_cli_name("makemkv.exe") is False
    assert is_makemkv_cli_name("not-makemkvcon64.exe") is False


def test_command_scope_distinguishes_local_drive_and_all_drive_work():
    assert makemkv_command_scope(("makemkvcon64.exe", "mkv", "disc:2", "all")) == (
        MakeMKVCommandScope("drive", 2)
    )
    assert makemkv_command_scope(("makemkvcon64.exe", "info", "disc:9999")) == (
        MakeMKVCommandScope("all-drives")
    )
    assert makemkv_command_scope(("makemkvcon64.exe", "mkv", "iso:C:/disc.iso")) == (
        MakeMKVCommandScope("none")
    )


def test_hardware_arbiter_allows_different_drives_and_refuses_overlap():
    arbiter = MakeMKVHardwareArbiter()
    drive_zero = arbiter.claim(MakeMKVCommandScope("drive", 0))
    drive_one = arbiter.claim(MakeMKVCommandScope("drive", 1))

    with pytest.raises(MakeMKVProcessControlError, match="already owns"):
        arbiter.claim(MakeMKVCommandScope("drive", 0))
    with pytest.raises(MakeMKVProcessControlError, match="All-drive"):
        arbiter.claim(MakeMKVCommandScope("all-drives"))

    assert drive_zero is not None
    assert drive_one is not None
    assert arbiter.active_drive_indexes() == (0, 1)
    drive_zero.close()
    drive_one.close()
    assert arbiter.active_drive_indexes() == ()


def test_hardware_lease_releases_only_after_child_exit_is_proven(monkeypatch):
    processes = [_Process(), _Process()]

    class Control:
        def start(self):
            return StartupCleanupResult(0, 0, 2)

    class Supervisor:
        def start(self, _command, **_kwargs):
            return processes.pop(0)

    monkeypatch.setattr(makemkv_process_control, "_MAKEMKV_STARTUP_CONTROL", Control())
    monkeypatch.setattr(
        makemkv_process_control, "_MAKEMKV_JOB_SUPERVISOR", Supervisor()
    )
    monkeypatch.setattr(makemkv_process_control, "_MAKEMKV_LAUNCHES_CLOSED", False)

    first = makemkv_process_control.start_makemkv_process((
        "makemkvcon64.exe",
        "mkv",
        "disc:0",
        "all",
        "output",
    ))
    makemkv_process_control.audit_makemkv_process_exit(first)
    with pytest.raises(MakeMKVProcessControlError, match="already owns"):
        makemkv_process_control.start_makemkv_process((
            "makemkvcon64.exe",
            "info",
            "disc:0",
        ))

    first.returncode = 0
    makemkv_process_control.audit_makemkv_process_exit(first)
    second = makemkv_process_control.start_makemkv_process((
        "makemkvcon64.exe",
        "info",
        "disc:0",
    ))
    assert second is not first


def test_startup_cleanup_terminates_every_cli_and_verifies_twice():
    alive = {
        10: "makemkvcon64.exe",
        11: "MAKEMKVCON.EXE",
        12: "unrelated.exe",
    }
    terminated = []
    sleeps = []

    def list_processes():
        return tuple(
            ProcessSnapshotEntry(process_id, executable_name)
            for process_id, executable_name in alive.items()
        )

    def terminate(process_id, _timeout):
        terminated.append(process_id)
        alive.pop(process_id)

    result = cleanup_existing_makemkv_processes(
        list_processes=list_processes,
        terminate_and_wait=terminate,
        sleeper=sleeps.append,
        verification_delay_seconds=0.01,
    )

    assert terminated == [10, 11]
    assert alive == {12: "unrelated.exe"}
    assert sleeps == [0.01, 0.01]
    assert result == StartupCleanupResult(2, 2, 2)


def test_startup_cleanup_fails_closed_when_cli_survives():
    def list_processes():
        return (ProcessSnapshotEntry(10, "makemkvcon64.exe"),)

    with pytest.raises(MakeMKVProcessControlError, match="remain"):
        cleanup_existing_makemkv_processes(
            list_processes=list_processes,
            terminate_and_wait=lambda *_args: None,
            sleeper=lambda _seconds: None,
            verification_delay_seconds=0,
        )


class _Lease:
    def __init__(self, events):
        self.events = events

    def close(self):
        self.events.append("release")


def test_startup_control_acquires_mutex_before_cleanup_and_retains_it():
    events = []
    control = ExclusiveMakeMKVStartupControl(
        acquire_mutex=lambda: (events.append("acquire"), _Lease(events))[1],
        cleanup=lambda: (
            events.append("cleanup"),
            StartupCleanupResult(1, 1, 2),
        )[1],
    )

    assert control.start() == StartupCleanupResult(1, 1, 2)
    assert control.start() == StartupCleanupResult(1, 1, 2)
    assert events == ["acquire", "cleanup"]

    control.close()
    assert events == ["acquire", "cleanup", "release"]


def test_startup_control_releases_mutex_when_cleanup_fails():
    events = []

    def fail():
        events.append("cleanup")
        raise MakeMKVProcessControlError("synthetic")

    control = ExclusiveMakeMKVStartupControl(
        acquire_mutex=lambda: (events.append("acquire"), _Lease(events))[1],
        cleanup=fail,
    )

    with pytest.raises(MakeMKVProcessControlError, match="synthetic"):
        control.start()

    assert events == ["acquire", "cleanup", "release"]


class _Process:
    def __init__(self):
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 1

    def kill(self):
        self.killed = True
        self.returncode = 1

    def wait(self, timeout=None):
        del timeout
        return self.returncode


class _Job:
    def __init__(self, events, *, fail_assignment=False):
        self.events = events
        self.fail_assignment = fail_assignment
        self.processes = []

    def assign(self, process):
        self.events.append(("assign", process))
        if self.fail_assignment:
            raise MakeMKVProcessControlError("synthetic")
        self.processes.append(process)

    def close(self):
        self.events.append("close")
        for process in self.processes:
            process.returncode = 1


def test_supervisor_creates_job_before_start_and_assigns_child():
    events = []
    process = _Process()

    def job_factory():
        events.append("create-job")
        return _Job(events)

    def popen_factory(command, **kwargs):
        events.append(("start", tuple(command), kwargs))
        return process

    supervisor = MakeMKVJobSupervisor(enabled=True, job_factory=job_factory)

    selected = supervisor.start(
        ("makemkvcon64.exe", "info", "disc:0"),
        popen_factory=popen_factory,
        stdout=subprocess.PIPE,
    )

    assert selected is process
    assert events[0] == "create-job"
    assert events[1][0] == "start"
    assert events[2] == ("assign", process)


def test_supervisor_settles_child_when_job_assignment_fails():
    events = []
    process = _Process()
    supervisor = MakeMKVJobSupervisor(
        enabled=True,
        job_factory=lambda: _Job(events, fail_assignment=True),
    )

    with pytest.raises(OSError, match="containment"):
        supervisor.start(
            ("makemkvcon64.exe", "info", "disc:0"),
            popen_factory=lambda *_args, **_kwargs: process,
        )

    with pytest.raises(OSError, match="unavailable"):
        supervisor.start(
            ("makemkvcon64.exe", "info", "disc:0"),
            popen_factory=lambda *_args, **_kwargs: pytest.fail(
                "A poisoned supervisor must not launch another child"
            ),
        )

    assert process.terminated is True
    assert process.poll() == 1


def test_supervisor_shutdown_settles_three_contained_children():
    events = []
    job = _Job(events)
    processes = [_Process(), _Process(), _Process()]
    supervisor = MakeMKVJobSupervisor(enabled=True, job_factory=lambda: job)

    for drive_index, process in enumerate(processes):
        supervisor.start(
            ("makemkvcon64.exe", "mkv", f"disc:{drive_index}", "0", "output"),
            popen_factory=lambda *_args, selected=process, **_kwargs: selected,
        )

    result = supervisor.close()

    assert result == SupervisedShutdownResult(tracked_count=3, settled_count=3)
    assert events[-1] == "close"
    assert all(process.poll() == 1 for process in processes)


def test_process_control_shutdown_settles_children_before_releasing_mutex(
    monkeypatch,
):
    events = []

    class Control:
        def close(self):
            events.append("release-mutex")

    class Supervisor:
        def close(self):
            events.append("close-job")
            return SupervisedShutdownResult(tracked_count=3, settled_count=3)

    monkeypatch.setattr(makemkv_process_control, "_MAKEMKV_STARTUP_CONTROL", Control())
    monkeypatch.setattr(
        makemkv_process_control, "_MAKEMKV_JOB_SUPERVISOR", Supervisor()
    )
    monkeypatch.setattr(makemkv_process_control, "_MAKEMKV_LAUNCHES_CLOSED", False)

    result = makemkv_process_control.shutdown_makemkv_process_control()

    assert result == SupervisedShutdownResult(tracked_count=3, settled_count=3)
    assert events == ["close-job", "release-mutex"]
    assert makemkv_process_control._MAKEMKV_LAUNCHES_CLOSED is True


def test_new_backend_lifetime_reopens_launches_only_after_startup_cleanup(monkeypatch):
    events = []
    process = _Process()

    class Control:
        def start(self):
            events.append("startup-cleanup")
            return StartupCleanupResult(0, 0, 2)

        def close(self):
            events.append("release-mutex")

    class Supervisor:
        def start(self, command, **_kwargs):
            events.append(("launch", tuple(command)))
            return process

        def close(self):
            events.append("close-job")
            return SupervisedShutdownResult(tracked_count=0, settled_count=0)

    monkeypatch.setattr(makemkv_process_control, "_MAKEMKV_STARTUP_CONTROL", Control())
    monkeypatch.setattr(
        makemkv_process_control, "_MAKEMKV_JOB_SUPERVISOR", Supervisor()
    )
    monkeypatch.setattr(makemkv_process_control, "_MAKEMKV_LAUNCHES_CLOSED", False)

    makemkv_process_control.shutdown_makemkv_process_control()
    assert makemkv_process_control.start_makemkv_process_control() == (
        StartupCleanupResult(0, 0, 2)
    )
    selected = makemkv_process_control.start_makemkv_process((
        "makemkvcon64.exe",
        "info",
        "disc:0",
    ))

    assert selected is process
    assert events == [
        "close-job",
        "release-mutex",
        "startup-cleanup",
        "startup-cleanup",
        ("launch", ("makemkvcon64.exe", "info", "disc:0")),
    ]


def test_failed_shutdown_retains_mutex_and_refuses_new_children(monkeypatch):
    events = []

    class Control:
        def start(self):
            events.append("unexpected-start")

        def close(self):
            events.append("unexpected-release")

    class Supervisor:
        def start(self, *_args, **_kwargs):
            events.append("unexpected-child")

        def close(self):
            events.append("close-job")
            raise MakeMKVProcessControlError("synthetic unsettled child")

    monkeypatch.setattr(makemkv_process_control, "_MAKEMKV_STARTUP_CONTROL", Control())
    monkeypatch.setattr(
        makemkv_process_control, "_MAKEMKV_JOB_SUPERVISOR", Supervisor()
    )
    monkeypatch.setattr(makemkv_process_control, "_MAKEMKV_LAUNCHES_CLOSED", False)

    with pytest.raises(MakeMKVProcessControlError, match="unsettled"):
        makemkv_process_control.shutdown_makemkv_process_control()
    with pytest.raises(OSError, match="shutting down"):
        makemkv_process_control.start_makemkv_process(("makemkvcon64.exe",))

    assert events == ["close-job"]


def test_public_start_acquires_exclusivity_before_launch(monkeypatch):
    events = []
    process = _Process()

    class Control:
        def start(self):
            events.append("exclusive")

    class Supervisor:
        def start(self, command, **kwargs):
            events.append(("launch", tuple(command), kwargs))
            return process

    monkeypatch.setattr(makemkv_process_control, "_MAKEMKV_STARTUP_CONTROL", Control())
    monkeypatch.setattr(
        makemkv_process_control, "_MAKEMKV_JOB_SUPERVISOR", Supervisor()
    )

    selected = makemkv_process_control.start_makemkv_process(
        ("makemkvcon64.exe", "info", "disc:0"), stdout=subprocess.PIPE
    )

    assert selected is process
    assert events[0] == "exclusive"
    assert events[1][0] == "launch"


def test_process_audit_retains_only_counts_and_error_type(monkeypatch):
    records = []
    monkeypatch.setattr(
        makemkv_process_control.logger,
        "info",
        lambda message, *values: records.append((message, values)),
    )
    process = _Process()
    process.pid = 42
    process.args = (
        "C:/private/MakeMKV/makemkvcon64.exe",
        "mkv",
        "disc:2",
        "3",
        "C:/private/output",
    )

    makemkv_process_control.audit_makemkv_process_start(process, process.args)
    process.returncode = 1
    makemkv_process_control.audit_makemkv_process_exit(process)
    makemkv_process_control.audit_makemkv_process_exit(process)

    assert records == [
        (
            "MakeMKV child started; child_count=1",
            (),
        ),
        (
            "MakeMKV child exited; child_count=1 error_type={}",
            ("nonzero_exit",),
        ),
    ]
    assert "private" not in repr(records)
    assert "42" not in repr(records)


def test_supervised_timeout_preserves_output_collected_while_reaping(monkeypatch):
    command = ("makemkvcon64.exe", "-r", "--cache=1", "info", "disc:9999")

    class TimeoutProcess:
        returncode = None

        def __init__(self):
            self.communicate_calls = 0

        def communicate(self, *, timeout=None):
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                raise subprocess.TimeoutExpired(
                    command,
                    timeout,
                    output="partial",
                    stderr="",
                )
            return ("complete drive rows", "bounded diagnostic")

        def kill(self):
            self.returncode = -9

    process = TimeoutProcess()
    monkeypatch.setattr(
        makemkv_process_control,
        "start_makemkv_process",
        lambda *_args, **_kwargs: process,
    )
    monkeypatch.setattr(
        makemkv_process_control,
        "audit_makemkv_process_exit",
        lambda _process: None,
    )

    with pytest.raises(subprocess.TimeoutExpired) as exc_info:
        makemkv_process_control.run_makemkv_command(
            command,
            timeout_seconds=15,
        )

    assert exc_info.value.output == "complete drive rows"
    assert exc_info.value.stderr == "bounded diagnostic"
    assert process.returncode == -9
