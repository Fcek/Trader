import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import patch, AsyncMock
from backend.main import app
from backend.config import BOT_RECOVERY_CODE, ADMIN_PASSWORD

@pytest.fixture(autouse=True)
def mock_bot():
    with patch("backend.main.bot") as mock_bot_inst:
        mock_bot_inst.running = True
        mock_bot_inst.start = AsyncMock()
        mock_bot_inst.stop = AsyncMock()
        mock_bot_inst._close_position = AsyncMock()
        yield mock_bot_inst

@pytest.fixture(autouse=True)
def mock_db():
    with patch("backend.main.get_open_trades", return_value=[{"symbol": "AAPL"}]), \
         patch("backend.main.get_logs", return_value=[{"msg": "test log"}]), \
         patch("backend.main.get_equity_history", return_value=[{"equity": 10000}]):
        yield

@pytest.mark.asyncio
async def test_get_status():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/api/status")
    assert response.status_code == 200
    assert response.json() == {"running": True}

@pytest.mark.asyncio
async def test_get_positions():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/api/positions")
    assert response.status_code == 200
    assert response.json() == [{"symbol": "AAPL"}]

@pytest.mark.asyncio
async def test_close_position(mock_bot):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.post(f"/api/positions/AAPL/close?password={ADMIN_PASSWORD}")
    assert response.status_code == 200
    assert response.json()["status"] == "success"
    mock_bot._close_position.assert_called_once_with("AAPL")

@pytest.mark.asyncio
async def test_get_logs():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/api/logs")
    assert response.status_code == 200
    assert response.json() == [{"msg": "test log"}]

@pytest.mark.asyncio
async def test_get_equity():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/api/equity")
    assert response.status_code == 200
    assert response.json() == [{"equity": 10000}]
