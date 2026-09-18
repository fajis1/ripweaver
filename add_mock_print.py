import re

with open('tests/test_downstream_worker.py', 'r', encoding='utf-8') as f:
    code = f.read()

replacement = '''    def mock_routing_append(assessment, expected_revision=None):
        print("MOCK ROUTING APPEND CALLED!")
        from dataclasses import replace
        from mkv_episode_matcher.disc.routing import TitleEvidence
        sibling_assessment = replace(assessment, revision=assessment.revision, evidence=assessment.evidence + (TitleEvidence(title_index=2, source="content", status="supported", role="movie"),))
        try:
            real_routing_append(sibling_assessment, expected_revision=expected_revision)
        except Exception as e:
            print("SIBLING APPEND FAILED:", type(e), e)
        try:
            return real_routing_append(assessment, expected_revision=expected_revision)
        except Exception as e:
            print("ORIGINAL APPEND FAILED:", type(e), e)
            raise'''

code = code.replace('''    def mock_routing_append(assessment, expected_revision=None):
        print("MOCK ROUTING APPEND CALLED!")
        from dataclasses import replace
        from mkv_episode_matcher.disc.routing import TitleEvidence
        sibling_assessment = replace(assessment, revision=assessment.revision, evidence=assessment.evidence + (TitleEvidence(title_index=2, source="content", status="supported", role="movie"),))
        # this will bump the DB to expected_revision + 1 successfully
        real_routing_append(sibling_assessment, expected_revision=expected_revision)
        # then the worker's own append will fail because expected_revision is stale
        return real_routing_append(assessment, expected_revision=expected_revision)''', replacement)

with open('tests/test_downstream_worker.py', 'w', encoding='utf-8') as f:
    f.write(code)
