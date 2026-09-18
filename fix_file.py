with open('tests/test_downstream_worker.py', 'r', encoding='utf-8') as f:
    code = f.read()

code = code.replace('\\n\\nfrom types import SimpleNamespace', '\n\nfrom types import SimpleNamespace')

with open('tests/test_downstream_worker.py', 'w', encoding='utf-8') as f:
    f.write(code)
