import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from backend.alpaca_client import AlpacaClient

@pytest.fixture
def client():
    # Set paper trading to True implicitly in tests or through mock config
    with patch("backend.alpaca_client.ALPACA_API_KEY", "test_key"), \
         patch("backend.alpaca_client.ALPACA_API_SECRET", "test_secret"), \
         patch("backend.alpaca_client.ALPACA_PAPER_TRADING", True):
        return AlpacaClient()

@pytest.mark.asyncio
async def test_get_account(client):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"equity": "10000"}
    
    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp
    
    with patch("httpx.AsyncClient", return_value=mock_client) as mock_httpx:
        # We must mock __aenter__ and __aexit__ for async with
        mock_httpx.return_value.__aenter__.return_value = mock_client
        res = await client.get_account()
        
        assert res == {"equity": "10000"}
        mock_client.get.assert_called_once_with(
            "https://paper-api.alpaca.markets/v2/account",
            headers=client.headers,
            params=None
        )

@pytest.mark.asyncio
async def test_request_error(client):
    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.text = "Bad Request"
    
    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp
    
    with patch("httpx.AsyncClient", return_value=mock_client) as mock_httpx:
        mock_httpx.return_value.__aenter__.return_value = mock_client
        with pytest.raises(RuntimeError, match="Alpaca API 400"):
            await client.get_account()

@pytest.mark.asyncio
async def test_submit_order_market(client):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"id": "123"}
    
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp
    
    with patch("httpx.AsyncClient", return_value=mock_client) as mock_httpx:
        mock_httpx.return_value.__aenter__.return_value = mock_client
        res = await client.submit_order(
            symbol="AAPL",
            qty=1.5,
            side="buy"
        )
        assert res == {"id": "123"}
        mock_client.post.assert_called_once()
        args, kwargs = mock_client.post.call_args
        assert kwargs["json"]["symbol"] == "AAPL"
        assert kwargs["json"]["qty"] == "1.5"

@pytest.mark.asyncio
async def test_submit_order_trailing_stop(client):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"id": "124"}
    
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp
    
    with patch("httpx.AsyncClient", return_value=mock_client) as mock_httpx:
        mock_httpx.return_value.__aenter__.return_value = mock_client
        res = await client.submit_order(
            symbol="AAPL",
            qty=1,
            side="sell",
            order_type="trailing_stop",
            trail_price=2.50
        )
        assert res == {"id": "124"}
        args, kwargs = mock_client.post.call_args
        assert kwargs["json"]["trail_price"] == "2.5"

@pytest.mark.asyncio
async def test_get_historical_bars_multi(client):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"bars": {"AAPL": [{"c": 150}]}}
    
    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp
    
    with patch("httpx.AsyncClient", return_value=mock_client) as mock_httpx:
        mock_httpx.return_value.__aenter__.return_value = mock_client
        res = await client.get_historical_bars_multi(["AAPL"])
        assert res == {"AAPL": [{"c": 150}]}
        
        # Test error case
        mock_resp.status_code = 400
        res2 = await client.get_historical_bars_multi(["AAPL"])
        assert res2 == {}

@pytest.mark.asyncio
async def test_listen_trade_updates(client):
    """Test websockets connection and message parsing."""
    import json
    
    mock_ws = AsyncMock()
    # First recv: auth success
    # Second recv: trade update
    mock_ws.recv.side_effect = [
        json.dumps({"data": {"status": "authorized"}}),
        json.dumps({"stream": "trade_updates", "data": {"event": "fill"}}),
        # Raise exception to break the infinite while loop in the test
        Exception("Break loop")
    ]
    
    callback_mock = AsyncMock()
    
    # We patch websockets.connect which is an async context manager
    with patch("websockets.connect", return_value=mock_ws) as mock_connect:
        mock_connect.return_value.__aenter__.return_value = mock_ws
        
        import asyncio
        with patch("asyncio.sleep", side_effect=asyncio.CancelledError):
            with pytest.raises(asyncio.CancelledError):
                await client.listen_trade_updates(callback_mock)
                
        callback_mock.assert_called_once_with({"event": "fill"})
