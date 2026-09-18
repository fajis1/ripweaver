with open('tests/test_downstream_worker.py', 'r', encoding='utf-8') as f:
    code = f.read()

code = code.replace('store.initialize()', '')

with open('tests/test_downstream_worker.py', 'w', encoding='utf-8') as f:
    f.write(code)
