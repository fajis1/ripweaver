import subprocess

original_code = subprocess.check_output(['git', 'show', '8fa3a10:tests/test_downstream_worker.py']).decode('utf-8')

with open('tests/test_downstream_worker.py', 'r', encoding='utf-8') as f:
    current_code = f.read()

# Extract the new tests from current_code
new_tests_code = current_code.split('def test_m5_sibling_revision_idempotent_retry(')[1]
new_tests_code = 'def test_m5_sibling_revision_idempotent_retry(' + new_tests_code

with open('tests/test_downstream_worker.py', 'w', encoding='utf-8') as f:
    f.write(original_code + '\n\n' + new_tests_code)
