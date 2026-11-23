import pytest

from database.local import LocalStore


@pytest.fixture
def store():
    s = LocalStore(":memory:")
    yield s
    s.close()
