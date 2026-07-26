#!/usr/bin/env python3
"""
Zoho Mail OAuth2 one-time setup helper.

Steps:
  1. Go to https://api-console.zoho.com  →  Add Client  →  Self Client
  2. Under "Generate Code", enter scopes (comma-separated):
       ZohoMail.accounts.READ,ZohoMail.messages.READ
     Duration: 10 minutes
  3. Click Create  →  copy the generated code
  4. Run this script immediately (code expires in ~10 min)
"""

import sys
import json
import requests


DATACENTERS = {
    "1": {
        "label": "US   — zoho.com (most accounts)",
        "auth_url": "https://accounts.zoho.com/oauth/v2/token",
        "mail_url": "https://mail.zoho.com/api",
    },
    "2": {
        "label": "India — zoho.in",
        "auth_url": "https://accounts.zoho.in/oauth/v2/token",
        "mail_url": "https://mail.zoho.in/api",
    },
    "3": {
        "label": "EU   — zoho.eu",
        "auth_url": "https://accounts.zoho.eu/oauth/v2/token",
        "mail_url": "https://mail.zoho.eu/api",
    },
}


def main() -> None:
    print("=" * 55)
    print(" Zoho Mail OAuth2 Setup")
    print("=" * 55)
    print()
    print("Pre-requisites (do these FIRST before running):")
    print("  1. api-console.zoho.com → Add Client → Self Client")
    print("  2. 'Generate Code' with scopes:")
    print("       ZohoMail.accounts.READ,ZohoMail.messages.READ")
    print("  3. Duration: 10 minutes  →  click Create")
    print("  4. Copy the one-time code  →  come back here")
    print()

    # Datacenter
    print("Which Zoho datacenter?")
    for key, dc in DATACENTERS.items():
        print(f"  {key}. {dc['label']}")
    dc_choice = input("Enter 1, 2, or 3 [default 1]: ").strip() or "1"
    dc = DATACENTERS.get(dc_choice, DATACENTERS["1"])
    print(f"  → {dc['label']}\n")

    client_id     = input("Client ID:     ").strip()
    client_secret = input("Client Secret: ").strip()
    code          = input("One-time code: ").strip()

    if not all([client_id, client_secret, code]):
        print("\nError: all three values are required.")
        sys.exit(1)

    print("\nExchanging code for tokens …")
    resp = requests.post(
        dc["auth_url"],
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
        },
        timeout=15,
    )

    try:
        data = resp.json()
    except Exception:
        print(f"Unexpected response ({resp.status_code}):\n{resp.text}")
        sys.exit(1)

    if "error" in data or "refresh_token" not in data:
        print("\nToken exchange failed:")
        print(json.dumps(data, indent=2))
        print("\nCommon causes:")
        print("  - Code expired (they last ~10 min)")
        print("  - Wrong client_id / client_secret")
        print("  - Scopes not matching")
        sys.exit(1)

    refresh_token = data["refresh_token"]

    print("\n✓ Success!\n")
    print("Add these lines to your .env file:")
    print("-" * 45)
    print(f"ZOHO_CLIENT_ID={client_id}")
    print(f"ZOHO_CLIENT_SECRET={client_secret}")
    print(f"ZOHO_REFRESH_TOKEN={refresh_token}")
    print(f"ZOHO_MAIL_BASE_URL={dc['mail_url']}")
    print(f"ZOHO_AUTH_URL={dc['auth_url']}")
    print("-" * 45)
    print()
    print("Next step:")
    print("  python main.py --get-account-id")
    print()
    print("(This lists your Zoho mail accounts so you can get")
    print(" your ZOHO_ACCOUNT_ID for the .env file.)")


if __name__ == "__main__":
    main()
