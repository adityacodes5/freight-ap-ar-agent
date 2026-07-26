"""
QuickBooks Online API client — raw `requests`, matching the Zoho/Supabase style
elsewhere in this repo (no heavy SDK, no new dependencies).

One-time setup: run `qb_auth.py` to authorize a (sandbox) company and capture a
refresh token + realm id into `.env`. This client then refreshes the short-lived
access token on demand and talks to the QBO v3 REST API.

Scope for now (the "add invoices to QuickBooks" workflow): find-or-create a
Customer, then create an AR Invoice keyed by DocNumber. Multicurrency-aware via
CurrencyRef. Read-heavy queries + idempotent creates — no payment movement.

Everything is self-contained and reads its own QB_* env vars, so this experiment
stays fully isolated from the production agent (config.py is untouched).
"""
import base64
import os
import random
import time

import requests
from loguru import logger

TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
_BASES = {
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
    "production": "https://quickbooks.api.intuit.com",
}
MINOR_VERSION = "75"  # QBO API minor version
_RETRY_STATUS = {429, 500, 502, 503, 504}  # rate-limit / transient server errors
_HTTP_RETRIES = 5
_HTTP_TIMEOUT = 60  # sandbox can be slow; ReadTimeout on a single call must not abort a batch


class QuickBooksError(RuntimeError):
    pass


class QuickBooksClient:
    def __init__(self, client_id=None, client_secret=None, refresh_token=None,
                 realm_id=None, environment=None, require_config=True):
        self.client_id = client_id or os.getenv("QB_CLIENT_ID", "")
        self.client_secret = client_secret or os.getenv("QB_CLIENT_SECRET", "")
        self.refresh_token = refresh_token or os.getenv("QB_REFRESH_TOKEN", "")
        self.realm_id = realm_id or os.getenv("QB_REALM_ID", "")
        env = (environment or os.getenv("QB_ENVIRONMENT", "sandbox")).lower()
        self.environment = env if env in _BASES else "sandbox"
        self.base = _BASES[self.environment]
        self._access_token = ""
        self._access_expiry = 0.0
        self._item_cache: dict[str, dict] = {}
        self._home_currency = ""
        if require_config:
            missing = [k for k, v in {
                "QB_CLIENT_ID": self.client_id, "QB_CLIENT_SECRET": self.client_secret,
                "QB_REFRESH_TOKEN": self.refresh_token, "QB_REALM_ID": self.realm_id,
            }.items() if not v]
            if missing:
                raise QuickBooksError(
                    "QuickBooks not configured — missing "
                    + ", ".join(missing) + ". Run qb_auth.py first.")

    # ── auth ──────────────────────────────────────────────────────────────────
    def _basic_auth(self) -> str:
        raw = f"{self.client_id}:{self.client_secret}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def _http(self, method: str, url: str, **kwargs) -> requests.Response:
        """Single HTTP call with retry/backoff on timeouts, connection errors,
        and transient (429/5xx) responses — so one flaky call can't abort a
        long batch. Honors Retry-After when the server sends it."""
        kwargs.setdefault("timeout", _HTTP_TIMEOUT)
        last = None
        for attempt in range(_HTTP_RETRIES):
            try:
                r = requests.request(method, url, **kwargs)
            except requests.exceptions.RequestException as e:
                last = e
            else:
                if r.status_code not in _RETRY_STATUS:
                    return r
                last = QuickBooksError(f"{r.status_code}: {r.text[:200]}")
                ra = r.headers.get("Retry-After")
                if ra:
                    try:
                        time.sleep(min(float(ra), 30))
                        continue
                    except ValueError:
                        pass
            if attempt < _HTTP_RETRIES - 1:
                time.sleep(min(2 ** attempt, 20) + random.random())
        raise QuickBooksError(f"Request to {url} failed after {_HTTP_RETRIES} tries: {last}")

    def _refresh_access(self) -> None:
        r = self._http(
            "POST", TOKEN_URL,
            headers={"Authorization": self._basic_auth(),
                     "Accept": "application/json",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "refresh_token", "refresh_token": self.refresh_token},
            timeout=20,
        )
        if r.status_code != 200:
            raise QuickBooksError(f"Token refresh failed ({r.status_code}): {r.text[:300]}")
        d = r.json()
        self._access_token = d["access_token"]
        # Intuit rotates the refresh token periodically; keep and surface the newest
        # so it can be written back to .env (old one stops working after rotation).
        new_refresh = d.get("refresh_token")
        if new_refresh and new_refresh != self.refresh_token:
            self.refresh_token = new_refresh
            logger.warning(
                "QuickBooks refresh token rotated — update QB_REFRESH_TOKEN in .env:\n{}",
                new_refresh)
        self._access_expiry = time.time() + int(d.get("expires_in", 3600)) - 60

    def _token(self) -> str:
        if not self._access_token or time.time() >= self._access_expiry:
            self._refresh_access()
        return self._access_token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token()}",
                "Accept": "application/json", "Content-Type": "application/json"}

    def _url(self, path: str) -> str:
        return f"{self.base}/v3/company/{self.realm_id}/{path}"

    # ── low-level ─────────────────────────────────────────────────────────────
    def query(self, sql: str) -> dict:
        r = self._http("GET", self._url("query"), headers=self._headers(),
                       params={"query": sql, "minorversion": MINOR_VERSION})
        if r.status_code != 200:
            raise QuickBooksError(f"Query failed ({r.status_code}): {r.text[:300]}")
        return r.json().get("QueryResponse", {})

    def _post(self, entity: str, payload: dict) -> dict:
        r = self._http("POST", self._url(entity), headers=self._headers(),
                       params={"minorversion": MINOR_VERSION}, json=payload)
        if r.status_code not in (200, 201):
            raise QuickBooksError(f"Create {entity} failed ({r.status_code}): {r.text[:400]}")
        return r.json()

    def company_info(self) -> dict:
        """Cheap connectivity check — returns the company name + home currency."""
        info = self.query("SELECT * FROM CompanyInfo").get("CompanyInfo", [{}])
        return info[0] if info else {}

    def home_currency(self) -> str:
        """The company file's home currency (cached). A customer/vendor created
        without an explicit CurrencyRef defaults to this, so we treat 'missing
        CurrencyRef' as the home currency when comparing."""
        if not self._home_currency:
            prefs = self.query("SELECT * FROM Preferences").get("Preferences", [{}])
            cp = (prefs[0] if prefs else {}).get("CurrencyPrefs", {})
            self._home_currency = cp.get("HomeCurrency", {}).get("value") or "USD"
        return self._home_currency

    def _party_currency(self, party: dict) -> str:
        return party.get("CurrencyRef", {}).get("value") or self.home_currency()

    def _fetch_rate(self, currency: str, as_of_date: str | None):
        params = {"sourcecurrencycode": currency, "minorversion": MINOR_VERSION}
        if as_of_date:
            params["asofdate"] = as_of_date
        r = self._http("GET", self._url("exchangerate"), headers=self._headers(), params=params)
        return r.json().get("ExchangeRate", {}).get("Rate") if r.status_code == 200 else None

    def exchange_rate(self, currency: str | None, as_of_date: str | None = None):
        """Home-currency units per one unit of `currency`, as QBO knows it.
        Returns None for the home currency (no rate needed). Cached by
        currency+date. Foreign-currency Bills require this explicitly."""
        if not currency or currency == self.home_currency():
            return None
        key = f"_fx_{currency}_{as_of_date or ''}"
        if key in self._item_cache:
            return self._item_cache[key]  # type: ignore[return-value]
        rate = self._fetch_rate(currency, as_of_date)
        if rate is None and as_of_date:
            # QBO has no rate for that date (e.g. future-dated loads) — fall back
            # to the current rate so the transaction still goes through.
            rate = self._fetch_rate(currency, None)
        self._item_cache[key] = rate
        return rate

    def _find_or_create_party(self, entity: str, key: str, name: str,
                              currency: str | None) -> dict:
        """Shared find-or-create for Customer/Vendor. QBO locks a party to ONE
        currency, but some customers/carriers trade in both CAD and USD — so if
        an existing party's currency differs from what this transaction needs,
        fall back to a currency-suffixed variant (e.g. 'New York Cables (CAD)')
        instead of failing the transaction."""
        want = currency or self.home_currency()
        existing = getattr(self, f"find_{key}")(name)
        if existing and self._party_currency(existing) == want:
            return existing
        if existing:  # currency mismatch → use/create a suffixed variant
            alt = f"{name} ({want})"
            existing_alt = getattr(self, f"find_{key}")(alt)
            if existing_alt:
                return existing_alt
            name = alt
        payload: dict = {"DisplayName": name}
        if currency:
            payload["CurrencyRef"] = {"value": currency}
        logger.info("QB: creating {} '{}'{}", key, name, f" ({currency})" if currency else "")
        return self._post(entity, payload)[entity.capitalize()]

    # ── customers ─────────────────────────────────────────────────────────────
    @staticmethod
    def _esc(s: str) -> str:
        return str(s).replace("\\", "\\\\").replace("'", "\\'")

    def find_customer(self, name: str) -> dict | None:
        resp = self.query(f"SELECT * FROM Customer WHERE DisplayName = '{self._esc(name)}'")
        rows = resp.get("Customer", [])
        return rows[0] if rows else None

    def find_or_create_customer(self, name: str, currency: str | None = None) -> dict:
        return self._find_or_create_party("customer", "customer", name, currency)

    # ── items (an Invoice line needs a Service item tied to an income account) ──
    def find_or_create_item(self, name: str = "Freight Brokerage") -> dict:
        if name in self._item_cache:
            return self._item_cache[name]
        resp = self.query(f"SELECT * FROM Item WHERE Name = '{self._esc(name)}'")
        rows = resp.get("Item", [])
        if rows:
            self._item_cache[name] = rows[0]
            return rows[0]
        acc = self.query("SELECT * FROM Account WHERE AccountType = 'Income' MAXRESULTS 1").get("Account", [])
        if not acc:
            raise QuickBooksError("No Income account found to attach the item to.")
        payload = {"Name": name, "Type": "Service",
                   "IncomeAccountRef": {"value": acc[0]["Id"]}}
        logger.info("QB: creating service item '{}'", name)
        item = self._post("item", payload)["Item"]
        self._item_cache[name] = item
        return item

    # ── invoices ────────────────────────────────────────────────────────────────
    def find_invoice(self, doc_number: str) -> dict | None:
        resp = self.query(f"SELECT * FROM Invoice WHERE DocNumber = '{self._esc(doc_number)}'")
        rows = resp.get("Invoice", [])
        return rows[0] if rows else None

    def create_invoice(self, *, customer_ref: str, lines: list[dict], item_ref: str,
                        doc_number: str | None = None, txn_date: str | None = None,
                        due_date: str | None = None, currency: str | None = None,
                        exchange_rate=None, memo: str | None = None) -> dict:
        """lines: [{"amount": float, "description": str}, ...]"""
        payload: dict = {
            "CustomerRef": {"value": customer_ref},
            "Line": [{
                "Amount": round(float(ln["amount"]), 2),
                "DetailType": "SalesItemLineDetail",
                "Description": ln.get("description", ""),
                "SalesItemLineDetail": {"ItemRef": {"value": item_ref}},
            } for ln in lines],
        }
        if doc_number:
            payload["DocNumber"] = str(doc_number)
        if txn_date:
            payload["TxnDate"] = txn_date
        if due_date:
            payload["DueDate"] = due_date
        if currency:
            payload["CurrencyRef"] = {"value": currency}
        if exchange_rate is not None:
            payload["ExchangeRate"] = exchange_rate
        if memo:
            payload["CustomerMemo"] = {"value": memo}
        return self._post("invoice", payload)["Invoice"]

    # ── vendors (carriers) ──────────────────────────────────────────────────────
    def find_vendor(self, name: str) -> dict | None:
        resp = self.query(f"SELECT * FROM Vendor WHERE DisplayName = '{self._esc(name)}'")
        rows = resp.get("Vendor", [])
        return rows[0] if rows else None

    def find_or_create_vendor(self, name: str, currency: str | None = None) -> dict:
        return self._find_or_create_party("vendor", "vendor", name, currency)

    def _accounts(self) -> list:
        """All accounts (cached once). We filter client-side because QBO rejects a
        WHERE filter on AccountType for some enum values ('CostOfGoodsSold' → code
        2010), so a single fetch + Python selection is the reliable path."""
        if "_accts" not in self._item_cache:
            self._item_cache["_accts"] = self.query(
                "SELECT * FROM Account MAXRESULTS 1000").get("Account", [])  # type: ignore[assignment]
        return self._item_cache["_accts"]  # type: ignore[return-value]

    def find_expense_account(self) -> dict:
        """A Bill line needs an expense/COGS account. Carrier cost is COGS for a
        broker, so prefer that; fall back to a plain Expense account."""
        if "_expense" in self._item_cache:
            return self._item_cache["_expense"]
        accts = self._accounts()

        def pick(pred):
            return next((a for a in accts if pred((a.get("AccountType") or ""))), None)

        acc = (pick(lambda t: t.replace(" ", "").lower() == "costofgoodssold")
               or pick(lambda t: t.lower() == "expense")
               or pick(lambda t: "expense" in t.lower()))
        if not acc:
            raise QuickBooksError("No expense/COGS account found for bill lines.")
        self._item_cache["_expense"] = acc
        return acc

    def find_bank_account(self, currency: str | None = None) -> dict:
        """A check Bill Payment must draw on a Bank account. Prefer one matching
        the payment currency (QBO multicurrency locks a bank account to one
        currency); fall back to any bank account."""
        accts = self._accounts()
        banks = [a for a in accts if (a.get("AccountType") or "").lower() == "bank"]
        if currency:
            match = next((a for a in banks
                          if (a.get("CurrencyRef", {}).get("value") or self.home_currency()) == currency), None)
            if match:
                return match
        if not banks:
            raise QuickBooksError("No bank account found for bill payments.")
        return banks[0]

    # ── bills (carrier AP) ──────────────────────────────────────────────────────
    def find_bill(self, doc_number: str) -> dict | None:
        resp = self.query(f"SELECT * FROM Bill WHERE DocNumber = '{self._esc(doc_number)}'")
        rows = resp.get("Bill", [])
        return rows[0] if rows else None

    def create_bill(self, *, vendor_ref: str, lines: list[dict], account_ref: str,
                    doc_number: str | None = None, txn_date: str | None = None,
                    due_date: str | None = None, currency: str | None = None,
                    exchange_rate=None, memo: str | None = None) -> dict:
        """lines: [{"amount": float, "description": str}, ...]"""
        payload: dict = {
            "VendorRef": {"value": vendor_ref},
            "Line": [{
                "Amount": round(float(ln["amount"]), 2),
                "DetailType": "AccountBasedExpenseLineDetail",
                "Description": ln.get("description", ""),
                "AccountBasedExpenseLineDetail": {"AccountRef": {"value": account_ref}},
            } for ln in lines],
        }
        if doc_number:
            payload["DocNumber"] = str(doc_number)
        if txn_date:
            payload["TxnDate"] = txn_date
        if due_date:
            payload["DueDate"] = due_date
        if currency:
            payload["CurrencyRef"] = {"value": currency}
        if exchange_rate is not None:
            payload["ExchangeRate"] = exchange_rate
        if memo:
            payload["PrivateNote"] = memo
        return self._post("bill", payload)["Bill"]

    # ── payments (apply cash to an invoice / bill) ──────────────────────────────
    def create_payment(self, *, customer_ref: str, invoice_id: str, amount: float,
                        txn_date: str | None = None, currency: str | None = None,
                        exchange_rate=None) -> dict:
        """Customer payment applied to one invoice (AR). Deposits to Undeposited
        Funds by default — the bank-rec step later moves it to the bank."""
        payload: dict = {
            "CustomerRef": {"value": customer_ref},
            "TotalAmt": round(float(amount), 2),
            "Line": [{"Amount": round(float(amount), 2),
                      "LinkedTxn": [{"TxnId": invoice_id, "TxnType": "Invoice"}]}],
        }
        if txn_date:
            payload["TxnDate"] = txn_date
        if currency:
            payload["CurrencyRef"] = {"value": currency}
        if exchange_rate is not None:
            payload["ExchangeRate"] = exchange_rate
        return self._post("payment", payload)["Payment"]

    def create_bill_payment(self, *, vendor_ref: str, bill_id: str, amount: float,
                            bank_account_ref: str, txn_date: str | None = None,
                            currency: str | None = None, exchange_rate=None) -> dict:
        """Payment applied to one carrier bill (AP), drawn on a bank account as a
        check (matches the ACH/Check tab)."""
        payload: dict = {
            "VendorRef": {"value": vendor_ref},
            "TotalAmt": round(float(amount), 2),
            "PayType": "Check",
            "CheckPayment": {"BankAccountRef": {"value": bank_account_ref}},
            "Line": [{"Amount": round(float(amount), 2),
                      "LinkedTxn": [{"TxnId": bill_id, "TxnType": "Bill"}]}],
        }
        if txn_date:
            payload["TxnDate"] = txn_date
        if currency:
            payload["CurrencyRef"] = {"value": currency}
        if exchange_rate is not None:
            payload["ExchangeRate"] = exchange_rate
        return self._post("billpayment", payload)["BillPayment"]
