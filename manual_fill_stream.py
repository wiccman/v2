"""Wake the single exit worker on Kalshi fills. Only REST reconciliation can authorize a sell."""
import json
import threading

from websockets.sync.client import connect


class ManualFillStream:
    def __init__(self, client, allowed, emit, stop):
        self.client, self.allowed, self.emit, self.stop = client, allowed, emit, stop
        self._tickers = set()
        self._lock = threading.Lock()
        self.wake = threading.Event()

    def take(self):
        with self._lock:
            tickers, self._tickers = self._tickers, set()
            self.wake.clear()
            return tickers

    def receive(self, raw):
        event = json.loads(raw)
        data = event.get("msg", {})
        if event.get("type") == "error":
            raise ValueError("Fill subscription rejected")
        if event.get("type") == "subscribed" and data.get("channel") == "fill":
            self.emit("MANUAL_FILL_STREAM_READY")
        if event.get("type") != "fill" or data.get("subaccount", 0) != 0:
            return
        ticker = data.get("market_ticker")
        if self.allowed(ticker):
            with self._lock:
                self._tickers.add(ticker)
                self.wake.set()

    def run(self):
        delay = 1
        while not self.stop.is_set():
            try:
                headers = self.client.signed_headers("GET", "/trade-api/ws/v2")
                with connect("wss://external-api-ws.kalshi.com/trade-api/ws/v2",
                             additional_headers=headers, open_timeout=5, close_timeout=1) as ws:
                    ws.send(json.dumps({"id": 1, "cmd": "subscribe", "params": {"channels": ["fill"]}}))
                    while not self.stop.is_set():
                        try:
                            raw = ws.recv(timeout=1)
                        except TimeoutError:
                            continue
                        self.receive(raw)
                        delay = 1
            except Exception as error:
                self.emit("MANUAL_FILL_STREAM_RETRY", error_type=type(error).__name__, delay_seconds=delay)
                self.stop.wait(delay)
                delay = min(30, delay * 2)
