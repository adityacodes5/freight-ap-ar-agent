"""Offline tests for the QuickBooks client + Accounts→QB mapping.

No network: the QBO query/create calls are monkeypatched, so these assert the
payload shape and idempotency logic without a live sandbox.
"""
import sys
import types

import pytest

from tools.quickbooks import QuickBooksClient, QuickBooksError


def _client():
    return QuickBooksClient(client_id="id", client_secret="secret",
                            refresh_token="rt", realm_id="123", environment="sandbox")


def test_missing_config_raises(monkeypatch):
    # Ensure a populated .env doesn't leak in via the os.getenv fallback.
    for k in ("QB_CLIENT_ID", "QB_CLIENT_SECRET", "QB_REFRESH_TOKEN", "QB_REALM_ID"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(QuickBooksError):
        QuickBooksClient(client_id="", client_secret="", refresh_token="", realm_id="")


def test_require_config_false_constructs():
    c = QuickBooksClient(require_config=False)
    assert c.environment == "sandbox"
    assert c.base.startswith("https://sandbox-")


def test_unknown_environment_falls_back_to_sandbox():
    c = _client()
    c2 = QuickBooksClient(client_id="i", client_secret="s", refresh_token="r",
                          realm_id="1", environment="prod-typo")
    assert c2.environment == "sandbox"


def test_esc_escapes_quotes_and_backslashes():
    assert QuickBooksClient._esc("O'Brien") == "O\\'Brien"
    assert QuickBooksClient._esc("a\\b") == "a\\\\b"


def test_create_invoice_payload_shape(monkeypatch):
    c = _client()
    captured = {}

    def fake_post(entity, payload):
        captured["entity"] = entity
        captured["payload"] = payload
        return {"Invoice": {"Id": "42", **payload}}

    monkeypatch.setattr(c, "_post", fake_post)
    inv = c.create_invoice(
        customer_ref="7", item_ref="9",
        lines=[{"amount": 2310.0, "description": "Freight — load 4779"},
               {"amount": 300.3, "description": "HST"}],
        doc_number="4779", txn_date="2026-01-07", due_date="2026-02-06",
        currency="CAD", memo="Load 4779",
    )
    assert inv["Id"] == "42"
    p = captured["payload"]
    assert captured["entity"] == "invoice"
    assert p["CustomerRef"] == {"value": "7"}
    assert p["DocNumber"] == "4779"
    assert p["TxnDate"] == "2026-01-07"
    assert p["DueDate"] == "2026-02-06"
    assert p["CurrencyRef"] == {"value": "CAD"}
    assert p["CustomerMemo"] == {"value": "Load 4779"}
    assert len(p["Line"]) == 2
    line = p["Line"][0]
    assert line["Amount"] == 2310.0
    assert line["DetailType"] == "SalesItemLineDetail"
    assert line["SalesItemLineDetail"]["ItemRef"] == {"value": "9"}


def test_find_invoice_idempotency(monkeypatch):
    c = _client()
    monkeypatch.setattr(c, "query", lambda sql: {"Invoice": [{"Id": "1", "DocNumber": "4779"}]})
    assert c.find_invoice("4779")["Id"] == "1"
    monkeypatch.setattr(c, "query", lambda sql: {})
    assert c.find_invoice("nope") is None


def test_find_or_create_customer_reuses_existing(monkeypatch):
    c = _client()
    monkeypatch.setattr(c, "find_customer",
                        lambda name: {"Id": "5", "DisplayName": name, "CurrencyRef": {"value": "CAD"}})
    called = {"post": False}
    monkeypatch.setattr(c, "_post", lambda *a: called.__setitem__("post", True))
    cust = c.find_or_create_customer("Absolute Lumber", "CAD")
    assert cust["Id"] == "5"
    assert called["post"] is False  # did not create a duplicate


def test_find_or_create_customer_currency_mismatch_uses_suffix(monkeypatch):
    """A USD customer + a CAD invoice must not fail — QBO locks a party to one
    currency, so we create a 'Name (CAD)' variant instead."""
    c = _client()

    def fake_find(name):
        if name == "New York Cables":
            return {"Id": "5", "DisplayName": name, "CurrencyRef": {"value": "USD"}}
        return None  # the (CAD) variant doesn't exist yet

    captured = {}
    monkeypatch.setattr(c, "find_customer", fake_find)
    monkeypatch.setattr(c, "_post",
                        lambda entity, payload: captured.update(payload) or {"Customer": {"Id": "6", **payload}})
    cust = c.find_or_create_customer("New York Cables", "CAD")
    assert cust["Id"] == "6"
    assert captured["DisplayName"] == "New York Cables (CAD)"
    assert captured["CurrencyRef"] == {"value": "CAD"}


def test_http_retries_then_succeeds(monkeypatch):
    import requests
    from tools import quickbooks as qb
    monkeypatch.setattr(qb.time, "sleep", lambda *a: None)  # no real backoff wait
    c = _client()
    calls = {"n": 0}

    class _Resp:
        status_code = 200
        text = "ok"

    def flaky(method, url, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.exceptions.ReadTimeout("boom")  # the exact failure that killed the batch
        return _Resp()

    monkeypatch.setattr(qb.requests, "request", flaky)
    r = c._http("GET", "https://x")
    assert r.status_code == 200 and calls["n"] == 2  # retried once, then ok


def test_http_raises_after_exhausting_retries(monkeypatch):
    import requests
    from tools import quickbooks as qb
    monkeypatch.setattr(qb.time, "sleep", lambda *a: None)
    c = _client()
    monkeypatch.setattr(qb.requests, "request",
                        lambda *a, **k: (_ for _ in ()).throw(requests.exceptions.ConnectionError("nope")))
    with pytest.raises(QuickBooksError):
        c._http("GET", "https://x")


def test_exchange_rate_home_currency_is_none(monkeypatch):
    c = _client()
    c._home_currency = "USD"  # avoid a Preferences round-trip
    assert c.exchange_rate("USD", "2026-01-13") is None
    assert c.exchange_rate(None) is None


def test_exchange_rate_foreign_fetched_and_cached(monkeypatch):
    from tools import quickbooks as qb
    c = _client()
    c._home_currency = "USD"
    calls = {"n": 0}

    class _Resp:
        status_code = 200
        @staticmethod
        def json():
            return {"ExchangeRate": {"Rate": 0.7201}}

    def fake_http(method, url, **kwargs):
        calls["n"] += 1
        return _Resp()

    monkeypatch.setattr(c, "_http", fake_http)
    monkeypatch.setattr(c, "_headers", lambda: {})
    assert c.exchange_rate("CAD", "2026-01-13") == 0.7201
    assert c.exchange_rate("CAD", "2026-01-13") == 0.7201  # cached
    assert calls["n"] == 1


def test_exchange_rate_future_date_falls_back_to_current(monkeypatch):
    c = _client()
    c._home_currency = "USD"

    class _Resp:
        def __init__(self, rate):
            self.status_code = 200
            self._rate = rate
        def json(self):
            return {"ExchangeRate": {"Rate": self._rate}}

    seen = []

    def fake_http(method, url, **kwargs):
        asof = kwargs.get("params", {}).get("asofdate")
        seen.append(asof)
        return _Resp(None if asof else 0.73)  # no rate for the future date; current works

    monkeypatch.setattr(c, "_http", fake_http)
    monkeypatch.setattr(c, "_headers", lambda: {})
    assert c.exchange_rate("CAD", "2026-08-05") == 0.73
    assert seen == ["2026-08-05", None]  # tried the date, then fell back to current


def test_create_bill_includes_exchange_rate_when_given(monkeypatch):
    c = _client()
    captured = {}
    monkeypatch.setattr(c, "_post", lambda entity, payload: captured.update(payload) or {"Bill": {"Id": "1"}})
    c.create_bill(vendor_ref="1", account_ref="2",
                  lines=[{"amount": 100.0, "description": "x"}],
                  currency="CAD", exchange_rate=0.7201)
    assert captured["ExchangeRate"] == 0.7201
    assert captured["CurrencyRef"] == {"value": "CAD"}


def test_create_bill_payload_shape(monkeypatch):
    c = _client()
    captured = {}
    monkeypatch.setattr(c, "_post", lambda entity, payload: captured.update(
        {"entity": entity, "payload": payload}) or {"Bill": {"Id": "88", **payload}})
    bill = c.create_bill(
        vendor_ref="12", account_ref="80",
        lines=[{"amount": 1800.0, "description": "Freight — load 4779"}],
        doc_number="M90", txn_date="2026-01-07", due_date="2026-02-02",
        currency="CAD", memo="Load 4779",
    )
    assert bill["Id"] == "88"
    p = captured["payload"]
    assert captured["entity"] == "bill"
    assert p["VendorRef"] == {"value": "12"}
    assert p["DocNumber"] == "M90"
    assert p["CurrencyRef"] == {"value": "CAD"}
    assert p["PrivateNote"] == "Load 4779"  # bills use PrivateNote, not CustomerMemo
    line = p["Line"][0]
    assert line["DetailType"] == "AccountBasedExpenseLineDetail"
    assert line["AccountBasedExpenseLineDetail"]["AccountRef"] == {"value": "80"}


def test_find_or_create_vendor_reuses_existing(monkeypatch):
    c = _client()
    monkeypatch.setattr(c, "find_vendor",
                        lambda name: {"Id": "9", "DisplayName": name, "CurrencyRef": {"value": "CAD"}})
    called = {"post": False}
    monkeypatch.setattr(c, "_post", lambda *a: called.__setitem__("post", True))
    v = c.find_or_create_vendor("10 WHEELS TRUCKING LTD", "CAD")
    assert v["Id"] == "9" and called["post"] is False


def test_find_expense_account_prefers_cogs(monkeypatch):
    c = _client()
    calls = []

    def fake_query(sql):
        calls.append(sql)
        # QBO returns AccountType with spaces; Expense listed first to prove COGS wins
        return {"Account": [
            {"Id": "90", "AccountType": "Expense"},
            {"Id": "80", "AccountType": "Cost of Goods Sold"},
        ]}

    monkeypatch.setattr(c, "query", fake_query)
    acc = c.find_expense_account()
    assert acc["Id"] == "80"  # COGS preferred even though Expense is first
    assert "AccountType" not in calls[0]  # no unsupported WHERE filter
    # cached — no second query
    assert c.find_expense_account()["Id"] == "80"
    assert len(calls) == 1


def test_create_payment_links_invoice(monkeypatch):
    c = _client()
    captured = {}
    monkeypatch.setattr(c, "_post", lambda e, p: captured.update(p) or {"Payment": {"Id": "1"}})
    c.create_payment(customer_ref="7", invoice_id="145", amount=950.0,
                     txn_date="2026-01-21", currency="USD")
    assert captured["TotalAmt"] == 950.0
    assert captured["CustomerRef"] == {"value": "7"}
    link = captured["Line"][0]["LinkedTxn"][0]
    assert link == {"TxnId": "145", "TxnType": "Invoice"}


def test_create_bill_payment_links_bill_and_bank(monkeypatch):
    c = _client()
    captured = {}
    monkeypatch.setattr(c, "_post", lambda e, p: captured.update(p) or {"BillPayment": {"Id": "1"}})
    c.create_bill_payment(vendor_ref="12", bill_id="1033", amount=2300.0,
                          bank_account_ref="35", currency="USD")
    assert captured["PayType"] == "Check"
    assert captured["CheckPayment"]["BankAccountRef"] == {"value": "35"}
    assert captured["Line"][0]["LinkedTxn"][0] == {"TxnId": "1033", "TxnType": "Bill"}


def test_find_bank_account_prefers_matching_currency(monkeypatch):
    c = _client()
    c._home_currency = "USD"
    c._item_cache["_accts"] = [
        {"Id": "35", "AccountType": "Bank", "CurrencyRef": {"value": "USD"}},
        {"Id": "36", "AccountType": "Bank", "CurrencyRef": {"value": "CAD"}},
        {"Id": "99", "AccountType": "Expense"},
    ]
    assert c.find_bank_account("CAD")["Id"] == "36"
    assert c.find_bank_account("USD")["Id"] == "35"
    # no CAD-less fallback still returns a bank
    c._item_cache["_accts"] = [{"Id": "35", "AccountType": "Bank", "CurrencyRef": {"value": "USD"}}]
    assert c.find_bank_account("CAD")["Id"] == "35"


def test_sync_lines_include_hst_only_when_present():
    # import lazily so the sheet client isn't constructed at module import
    if "sync_accounts_to_quickbooks" in sys.modules:
        del sys.modules["sync_accounts_to_quickbooks"]
    mod = __import__("sync_accounts_to_quickbooks")
    assert isinstance(mod, types.ModuleType)
    one = mod._lines({"shipper": 500.0, "hst": 0.0, "load": "4786"})
    assert len(one) == 1 and one[0]["amount"] == 500.0
    two = mod._lines({"shipper": 2310.0, "hst": 300.3, "load": "4779"})
    assert len(two) == 2 and two[1]["description"] == "HST"
