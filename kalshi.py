import base64
import os
import time
import uuid
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

import requests
from balance_diagnostics import balance_report
from request_coordinator import RequestCoordinator
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

class KalshiAPIError(RuntimeError):
    def __init__(self, status_code, message, retry_after=None, code=None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after
        self.code = code

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
    def __init__(self, key_id="", private_key_path="", private_key_b64="", timeout=20,
                 *, coordinator=None, role="entry", quote_ttl=0.5, clock=None):
        self.base = BASE_URL
        self.timeout = timeout
        self.key_id = key_id
        if role not in RequestCoordinator.PRIORITY_DELAY:
            raise ValueError("Unknown API worker role")
        self.coordinator = coordinator or RequestCoordinator()
        self.role = role
        self.quote_ttl = quote_ttl
        self.clock = clock or time.monotonic
        self._market_cache = {}
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
        # Do not wait here: an entry's deadline/quote must not age in a queue.
        # This exception proves no HTTP dispatch occurred, including for POST.
        self.coordinator.check(self.role)
        if method.upper() not in {"GET", "HEAD"}:
            self._market_cache.clear()
        response = requests.request(method, self.base + path, params=params, json=body,
            headers=self._headers(method, path) if auth else {}, timeout=self.timeout)
        try:
            response.raise_for_status()
        except requests.HTTPError as error:
            self._market_cache.clear()
            details = response.text.strip() or "<empty response>"
            retry_after = response.headers.get("Retry-After")
            if response.status_code == 429:
                retry_after = self.coordinator.limited(retry_after)
            code = None
            try:
                payload = response.json()
                error_body = payload.get("error", payload)
                candidate = error_body.get("code")
                if isinstance(candidate, str):
                    code = candidate
            except (ValueError, TypeError, AttributeError):
                pass
            raise KalshiAPIError(
                response.status_code, f"Kalshi API {response.status_code} {method.upper()} {path}: {details}",
                retry_after=retry_after, code=code,
            ) from error
        return response.json() if response.content else {}

    def markets(self, **params):
        return self.request("GET", "/markets", params=params).get("markets", [])

    def market(self, ticker):
        self.coordinator.check(self.role)
        now = self.clock()
        cached = self._market_cache.get(ticker)
        if cached and 0 <= now - cached[0] < self.quote_ttl:
            return dict(cached[1])
        self._market_cache.pop(ticker, None)
        market = self.request("GET", "/markets/" + ticker)["market"]
        # Age from request START: slow responses cannot extend quote freshness.
        self._market_cache[ticker] = (now, dict(market))
        return market

    def orderbook(self, ticker):
        return self.request("GET", "/markets/" + ticker + "/orderbook", auth=True)["orderbook_fp"]

    def order(self, order_id, ticker=None):
        """Resolve an order with market context; a 404 is never terminal proof."""
        params = {"market_ticker": ticker, "exchange_index": -1} if ticker else None
        try:
            order = self.request("GET", "/portfolio/orders/" + order_id,
                                 params=params, auth=True)["order"]
        except KalshiAPIError as error:
            if error.status_code != 404 or not ticker:
                raise
            # Read models may lag an IOC response. Search the paginated,
            # ticker-filtered order history before retrying on the next cycle.
            matches = [o for o in self.all_orders(ticker)
                       if o.get("order_id") == order_id and o.get("ticker") == ticker]
            if len(matches) != 1:
                raise error
            order = matches[0]
        if order.get("order_id") != order_id or (ticker and order.get("ticker") != ticker):
            raise RuntimeError("Order lookup identity mismatch")
        return order

    def all_orders(self, ticker, status=None):
        params = {"limit": 100}
        if ticker:
            # GetOrders is an aggregate read, not an API2 matching-engine
            # operation: it rejects negative exchange_index values. Omitting
            # that filter searches all shards while ticker scopes the market.
            # Keep API2 auto-routing on single-order/cancel calls separate.
            params["ticker"] = ticker
        if status is not None:
            params["status"] = status
        found, cursors = [], set()
        while True:
            result = self.request("GET", "/portfolio/orders", params=params, auth=True)
            found.extend(result["orders"])
            cursor = result.get("cursor")
            if not cursor:
                return found
            if cursor in cursors:
                raise RuntimeError("Order pagination cursor repeated")
            cursors.add(cursor)
            params["cursor"] = cursor

    def btc_reference_price(self):
        response = self.request("GET", "/cfbenchmarks/values", params={"id": "BRTI"}, auth=True)
        return _latest_index_value(response)

    def balance(self, exchange_index=None):
        params = {} if exchange_index is None else {"exchange_index": exchange_index}
        return self.request("GET", "/portfolio/balance", params=params, auth=True)

    def market_cash(self, ticker):
        """Read cash on the market's actual shard, never the aggregate wallet."""
        index = self.market(ticker).get("exchange_index")
        if type(index) is not int or index < 0:
            raise ValueError("Market exchange_index missing or invalid")
        return balance_report(self.balance(exchange_index=index), exchange_index=index)

    def subaccount_balances(self):
        return self.request("GET", "/portfolio/subaccounts/balances", auth=True)

    def positions(self, ticker=None):
        params = {"ticker": ticker} if ticker else None
        return self.request("GET", "/portfolio/positions", params=params, auth=True).get("market_positions", [])

    def fills(self, ticker=None):
        params = {"limit": 100}
        if ticker:
            params["ticker"] = ticker if ticker else {"limit": 100}
        return self.request("GET", "/portfolio/fills", params=params, auth=True).get("fills", [])

    def all_fills(self, ticker):
        """Complete primary-account history for paired inventory reconciliation."""
        params = {"ticker": ticker, "limit": 1000, "subaccount": 0}
        found, cursors = [], set()
        while True:
            result = self.request("GET", "/portfolio/fills", params=params, auth=True)
            found.extend(result["fills"])
            cursor = result.get("cursor")
            if not cursor:
                return found
            if cursor in cursors:
                raise RuntimeError("Fill pagination cursor repeated")
            cursors.add(cursor)
            params["cursor"] = cursor

    def orders(self, ticker=None, status="resting"):
        return self.all_orders(ticker, status)

    def _order(self, ticker, book_side, quantity, yes_price, reduce_only=False, expiration_time=None, ioc=False, post_only=False, client_order_id=None):
        body = {
            "ticker": ticker, "side": book_side, "count": format(Decimal(quantity), "f"),
            "price": format(Decimal(yes_price).quantize(Decimal("0.0001")), "f"),
            "time_in_force": "immediate_or_cancel" if ioc else "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross", "client_order_id": client_order_id or str(uuid.uuid4()),
            "post_only": post_only, "cancel_order_on_pause": True, "reduce_only": reduce_only,
        }
        if expiration_time is not None and not ioc:
            body["expiration_time"] = int(expiration_time)
        return self.request("POST", "/portfolio/events/orders", body=body, auth=True)

    def place_entry(self, ticker, prediction, quantity, outcome_price, expiration_time, *, submit_before=None, client_order_id=None, ioc=False):
        # A slow quote request or preceding order must not submit an expired entry.
        if time.time() >= min(expiration_time, submit_before if submit_before is not None else expiration_time):
            return {}
        outcome_price = Decimal(outcome_price)
        if prediction == "YES":
            return self._order(ticker, "bid", quantity, outcome_price, expiration_time=expiration_time, client_order_id=client_order_id, **({"ioc": True} if ioc else {}))
        if prediction == "NO":
            return self._order(ticker, "ask", quantity, Decimal("1") - outcome_price, expiration_time=expiration_time, client_order_id=client_order_id, **({"ioc": True} if ioc else {}))
        raise ValueError("prediction must be YES or NO")

    def close_position(self, ticker, signed_quantity, yes_bid, yes_ask):
        signed_quantity = Decimal(signed_quantity)
        if signed_quantity > 0:
            return self._order(ticker, "ask", signed_quantity, yes_bid, reduce_only=True, ioc=True)
        if signed_quantity < 0:
            return self._order(ticker, "bid", abs(signed_quantity), yes_ask, reduce_only=True, ioc=True)
        return {}

    def place_take_profit(self, ticker, signed_quantity, outcome_price, expiration_time=None, *, client_order_id=None):
        """Try a price-protected reduce-only IOC; caller monitors/retries leftovers."""
        signed_quantity = Decimal(signed_quantity)
        outcome_price = Decimal(outcome_price)
        if signed_quantity > 0:
            return self._order(
                ticker, "ask", signed_quantity, outcome_price,
                reduce_only=True, ioc=True, client_order_id=client_order_id,
            )
        if signed_quantity < 0:
            return self._order(
                ticker, "bid", abs(signed_quantity), Decimal("1") - outcome_price,
                reduce_only=True, ioc=True, client_order_id=client_order_id,
            )
        return {}

    def cancel(self, order_id, ticker):
        return self.request("DELETE", "/portfolio/events/orders/" + order_id,
                            params={"market_ticker": ticker, "exchange_index": -1}, auth=True)
