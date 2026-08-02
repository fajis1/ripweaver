from mkv_episode_matcher.backend.routers.system import _find_portable_executable


def test_portable_tool_discovery_is_exact_and_depth_bounded(tmp_path):
    shallow = tmp_path / "bundle" / "release"
    shallow.mkdir(parents=True)
    executable = shallow / "HandBrakeCLI.exe"
    executable.write_bytes(b"synthetic")
    (shallow / "HandBrakeCLI.exe.txt").write_text("not executable", encoding="utf-8")

    assert _find_portable_executable("HandBrakeCLI.exe", (tmp_path,)) == str(
        executable.resolve()
    )

    too_deep = tmp_path / "one" / "two" / "three" / "four"
    too_deep.mkdir(parents=True)
    (too_deep / "other.exe").write_bytes(b"synthetic")
    assert _find_portable_executable("other.exe", (tmp_path,), max_depth=2) is None
