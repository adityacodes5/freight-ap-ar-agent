#!/usr/bin/env python3
"""
QuickBooks Online OAuth2 one-time setup helper.

Prerequisites (do these FIRST at https://developer.intuit.com):
  1. Sign in → Dashboard → create an app → "QuickBooks Online and Payments".
  2. Keys & OAuth → Development keys → copy the Client ID + Client Secret.
  3. Under "Redirect URIs", add EXACTLY:  http://localhost:8000/callback
  4. Sandbox → confirm you have a sandbox company (create one if not), and in
     that company enable multicurrency:
        Gear ▸ Account and settings ▸ Advanced ▸ Currency ▸ Multicurrency ▸ On
        (pick your home currency, e.g. CAD — this can't be undone in that file).

Then run this script:
    python qb_auth.py
It opens the Intuit consent page, captures the redirect, exchanges the code, and
prints the QB_REFRESH_TOKEN + QB_REALM_ID lines to paste into your .env.
"""
import base64
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
REDIRECT_URI = "http://localhost:8000/callback"
SCOPE = "com.intuit.quickbooks.accounting"
STATE = "logistics-qb-setup"

_result: dict = {}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        qs = urllib.parse.urlparse(self.path).query
        params = dict(urllib.parse.parse_qsl(qs))
        _result.update(params)
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        ok = "code" in params and "realmId" in params
        msg = ("Authorized — you can close this tab and return to the terminal."
               if ok else "Missing code/realmId. Check the terminal.")
        self.wfile.write(f"<html><body><h3>{msg}</h3></body></html>".encode())

    def log_message(self, *a):  # silence the default request logging
        pass


def main() -> None:
    print("=" * 60)
    print(" QuickBooks Online OAuth2 Setup")
    print("=" * 60)
    print("\nRedirect URI must be registered on the app EXACTLY as:")
    print(f"    {REDIRECT_URI}\n")

    client_id = input("Client ID:     ").strip()
    client_secret = input("Client Secret: ").strip()
    if not client_id or not client_secret:
        print("\nError: Client ID and Client Secret are required.")
        sys.exit(1)

    authorize = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "scope": SCOPE,
        "redirect_uri": REDIRECT_URI,
        "state": STATE,
    })
    print("\nOpening the Intuit consent page in your browser…")
    print("If it doesn't open, paste this URL manually:\n")
    print(authorize + "\n")
    webbrowser.open(authorize)

    print("Waiting for the redirect on http://localhost:8000/callback …")
    server = HTTPServer(("localhost", 8000), _Handler)
    while "code" not in _result and "error" not in _result:
        server.handle_request()

    if "error" in _result:
        print(f"\nAuthorization failed: {_result.get('error')}")
        sys.exit(1)

    code = _result["code"]
    realm_id = _result.get("realmId", "")
    print(f"\nGot authorization code (realmId={realm_id}). Exchanging for tokens…")

    basic = "Basic " + base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(
        TOKEN_URL,
        headers={"Authorization": basic, "Accept": "application/json",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "authorization_code", "code": code,
              "redirect_uri": REDIRECT_URI},
        timeout=20,
    )
    if resp.status_code != 200:
        print(f"\nToken exchange failed ({resp.status_code}): {resp.text[:400]}")
        sys.exit(1)

    d = resp.json()
    refresh_token = d.get("refresh_token", "")
    print("\n" + "=" * 60)
    print(" SUCCESS — add these lines to your .env:")
    print("=" * 60)
    print(f"QB_CLIENT_ID={client_id}")
    print(f"QB_CLIENT_SECRET={client_secret}")
    print(f"QB_REFRESH_TOKEN={refresh_token}")
    print(f"QB_REALM_ID={realm_id}")
    print("QB_ENVIRONMENT=sandbox")
    print("=" * 60)
    print("\nThen test with:  python sync_accounts_to_quickbooks.py --check")


if __name__ == "__main__":
    main()
