import pytest

from app.db import connect, init_schema


@pytest.fixture()
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    conn = connect(path)
    init_schema(conn)
    conn.close()
    return path


@pytest.fixture()
def conn(db_path):
    c = connect(db_path)
    yield c
    c.close()
