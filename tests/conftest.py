import pytest

@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """
    Redirect DB_FILE_PATH to a fresh temporary file for every test.
    Clears backend.database._global_conn to ensure no DB state leaks across tests.
    """
    db_file = tmp_path / "test_trader.db"
    monkeypatch.setattr("backend.config.DB_FILE_PATH", db_file)
    monkeypatch.setattr("backend.database.DB_FILE_PATH", db_file)

    import backend.database
    if backend.database._global_conn is not None:
        try:
            backend.database._global_conn.close()
        except Exception:
            pass
    backend.database._global_conn = None

    backend.database.init_db()

    yield db_file
