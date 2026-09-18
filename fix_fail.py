import re

with open('tests/test_downstream_worker.py', 'r', encoding='utf-8') as f:
    code = f.read()

# Replace the mock_routing_append in the fail test to always conflict
replacement = '''      call_count = 0
      def mock_routing_append(assessment, expected_revision=None):
          nonlocal call_count
          call_count += 1
          from dataclasses import replace
          from mkv_episode_matcher.disc.routing import TitleEvidence
          sibling_assessment = replace(assessment, revision=assessment.revision, evidence=assessment.evidence + (TitleEvidence(title_index=2, source="content", status="supported", role="movie"),))
          real_routing_append(sibling_assessment, expected_revision=expected_revision)
          return real_routing_append(assessment, expected_revision=expected_revision)
          
      store.routing_append = mock_routing_append'''

old = '''      call_count = 0
      def mock_routing_append(assessment, expected_revision=None):
          nonlocal call_count
          call_count += 1
          if call_count == 1:
              from dataclasses import replace
              sibling_assessment = replace(initial_assessment, revision=initial_assessment.revision + 1, evidence=(TitleEvidence(title_index=1, source="content", status="supported", role="tv"),))
              real_routing_append(sibling_assessment, expected_revision=initial_assessment.revision)
          return real_routing_append(assessment, expected_revision=expected_revision)
          
      store.routing_append = mock_routing_append'''

code = code.replace(old, replacement)

with open('tests/test_downstream_worker.py', 'w', encoding='utf-8') as f:
    f.write(code)
