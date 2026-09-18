import re

with open('tests/test_downstream_worker.py', 'r', encoding='utf-8') as f:
    code = f.read()

replacement = '''    def mock_routing_append(assessment, expected_revision=None):
        print("MOCK ROUTING APPEND CALLED!")
        from dataclasses import replace'''

code = code.replace('''    def mock_routing_append(assessment, expected_revision=None):
        # Always inject a conflicting evidence that doesn't change the route for title_index=1
        from dataclasses import replace''', replacement)

with open('tests/test_downstream_worker.py', 'w', encoding='utf-8') as f:
    f.write(code)
