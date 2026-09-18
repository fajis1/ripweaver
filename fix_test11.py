with open('tests/test_downstream_worker.py', 'r', encoding='utf-8') as f:
    code = f.read()

code = code.replace(
    'initial_assessment = store.routing_append(initial_assessment)',
    'initial_assessment = store.routing_append(initial_assessment, expected_revision=0)'
)

with open('tests/test_downstream_worker.py', 'w', encoding='utf-8') as f:
    f.write(code)
