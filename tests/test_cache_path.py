from mkv_episode_matcher.core.utils import safe_cache_component


def test_safe_cache_component_preserves_identity_with_windows_safe_name():
    first = safe_cache_component("Dragons: Race to the Edge")
    second = safe_cache_component("Dragons: Race to the Edge")

    assert first == second
    assert ":" not in first
    assert first.startswith("Dragons_ Race to the Edge-")


def test_safe_cache_component_handles_reserved_and_empty_names():
    assert safe_cache_component("CON").startswith("_CON")
    assert safe_cache_component("   ")
