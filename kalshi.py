import base64
import os
import time
import uuid
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

def _latest_index_value(payload):
    """Extract the newest numeric index value from a CF Benchmarks response."""
    if isinstance(payload, dict):
        if "value" in payload and not isinstance(payload["value"], (dict, list)):
            return Decimal(str(payload["value"]))
        for key in ("payload", "data", "BRTI", "values"):
            if key in payload:
                try:
                    return _latest_index_value(payload[key])
                except (KeyError, TypeError, ValueError):
                    pass
    elif isinstance(payload, list) and payload:
        for item in reversed(payload):
            try:
                return _latest_index_value(item)
            except (KeyError, TypeError, ValueError):
                pass
    raise ValueError("Kalshi CF Benchmarks response did not contain a BRTI value")

class KalshiClient:
    def __init__(self, key_id="", private_key_path="", private_key_b64=""):
        self.base = BASE_URL
        self.key_id = key_id
        pem = b""
        if private_key_b64:
            pem = base64.b64decode(private_key_b64)
        elif private_key_path and Path(private_key_path).exists():
            pem = Path(private_key_path).read_bytes()
        elif os.getenv("KALSHI_PRIVATE_KEY_PEM"):
            pem = os.environ["KALSHI_PRIVATE_KEY_PEM"].replace("\\n", "\n").encode()
        self.private_key = serialization.load_pem_private_key(pem, password=None) if pem else None

    def _headers(self, method, path):
        if not self.key_id or self.private_key is None:
            raise RuntimeError("Kalshi credentials unavailable")
        timestamp = str(int(time.time() * 1000))
        signed_path = urlparse(self.base + path).path
        signature = self.private_key.sign(
            (timestamp + method.upper() + signed_path).encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
            "Content-Type": "application/json",
        }

    def request(self, method, path, params=None, body=None, auth=False):
        response = requests.request(method, self.base + path, params=params, json=body,
            headers=self._headers(method, path) if auth else {}, timeout=20)
        response.raise_for_status()
        return response.json() if response.content else {}

    def markets(self, **params):
        return self.request("GET", "/markets", params=params).get("markets", [])

    def market(self, ticker):
        return self.request("GET", "/markets/" + ticker)["market"]

    def btc_reference_price(self):
        response = self.request("GET", "/cfbenchmarks/values", params={"id": "BRTI"}, auth=True)
        return _latest_index_value(response)

    def balance(self):
        return self.request("GET", "/portfolio/balance", auth=True)

    def positions(self, ticker=None):
        params = {"ticker": ticker} if ticker else None
        return self.request("GET", "/portfolio/positions", params=params, auth=True).get("market_positions", [])

    def fills(self, ticker=None):
        params = {"ticker": ticker, "limit": 100} if ticker else {"limit": 100}
        return self.request("GET", "/portfolio/fills", params=params, auth=True).get("fills", [])

    def orders(self, ticker=None, status="resting"):
        params = {"status": status}
        if ticker:
            params["ticker"] = ticker
        return self.request("GET", "/portfolio/orders", params=params, auth=True).get("orders", [])

    def _order(self, ticker, book_side, quantity, yes_price, reduce_only=False, expiration_time=None, ioc=False):
        body = {
            "ticker": ticker, "side": book_side, "count": format(Decimal(quantity), "f"),
            "price": format(Decimal(yes_price).quantize(Decimal("0.0001")), "f"),
            "time_in_force": "immediate_or_cancel" if ioc else "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross", "client_order_id": str(uuid.uuid4()),
            "post_only": False, "cancel_order_on_pause": True, "reduce_only": reduce_only,
        }
        if expiration_time is not None and not ioc:
            body["expiration_time"] = int(expiration_time)
        return self.request("POST", "/portfolio/events/orders", body=body, auth=True)

    def place_entry(self, ticker, prediction, quantity, outcome_price, expiration_time):
        outcome_price = Decimal(outcome_price)
        if prediction == "YES":
            return self._order(ticker, "bid", quantity, outcome_price, expiration_time=expiration_time)
        if prediction == "NO":
            return self._order(ticker, "ask", quantity, Decimal("1") - outcome_price, expiration_time=expiration_time)
        raise ValueError("prediction must be YES or NO")

    def close_position(self, ticker, signed_quantity, yes_bid, yes_ask):
        signed_quantity = Decimal(signed_quantity)
        if signed_quantity > 0:
            return self._order(ticker, "ask", signed_quantity, yes_bid, reduce_only=True, ioc=True)
        if signed_quantity < 0:
            return self._order(ticker, "bid", abs(signed_quantity), yes_ask, reduce_only=True, ioc=True)
        return {}

    def cancel(self, order_id):
        return self.request("DELETE", "/portfolio/events/orders/" + order_id, auth=True)
