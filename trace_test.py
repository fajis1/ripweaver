import sys

def trace_calls_and_returns(frame, event, arg):
    if event in ('call', 'return', 'line') and 'test_m5' in frame.f_code.co_name:
        pass
    if event == 'line' and 'downstream_worker.py' in frame.f_code.co_filename:
        if frame.f_code.co_name == '_apply_automatic_assessed_gemini_route':
            print(f"LINE: {frame.f_lineno}")
    if event == 'return' and 'downstream_worker.py' in frame.f_code.co_filename:
        if frame.f_code.co_name == '_apply_automatic_assessed_gemini_route':
            print(f"RETURNED: {arg}")
    return trace_calls_and_returns

import pytest
sys.settrace(trace_calls_and_returns)
pytest.main(['-q', '-s', 'tests/test_downstream_worker.py::test_m5_sibling_revision_idempotent_retry_fail'])
