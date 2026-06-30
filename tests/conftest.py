import os
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent


@pytest.fixture(autouse=True, scope="session")
def set_project_root_cwd():
    original = os.getcwd()
    os.chdir(PROJECT_ROOT)
    yield
    os.chdir(original)
