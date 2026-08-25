from mkv_episode_matcher import tmdb_client


def test_movie_candidates_include_validated_runtime(monkeypatch):
    calls = []

    def fake_get(path, **parameters):
        calls.append((path, parameters))
        if path == "/search/movie":
            return {
                "results": [
                    {
                        "id": 123,
                        "title": "Search Title",
                        "release_date": "1966-08-03",
                    }
                ]
            }
        assert path == "/movie/123"
        return {
            "id": 123,
            "title": "The Man Called Flintstone",
            "original_title": "The Man Called Flintstone",
            "release_date": "1966-08-03",
            "overview": "A feature film related to the television series.",
            "runtime": 89,
        }

    monkeypatch.setattr(tmdb_client, "_tmdb_get_json", fake_get)

    candidates = tmdb_client.search_movie_candidates("The Flintstones")

    assert len(candidates) == 1
    assert candidates[0].tmdb_id == 123
    assert candidates[0].title == "The Man Called Flintstone"
    assert candidates[0].release_year == 1966
    assert candidates[0].runtime_seconds == 89 * 60
    assert calls == [
        ("/search/movie", {"query": "The Flintstones"}),
        ("/movie/123", {}),
    ]
