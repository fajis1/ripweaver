"""Global test isolation from developer and user credential files."""

import os

# Tests use explicit fake process-environment values. Never let the suite load
# the repository's real ignored .env file.
os.environ["MKV_MATCH_ENV_FILE"] = ""
