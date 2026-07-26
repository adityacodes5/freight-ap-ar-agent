"""
One-shot Zoho token exchange helper.
Run: python3 get_token.py <authorization_code>
Reads CLIENT_ID and CLIENT_SECRET from .env
"""
import sys
import json
import requests
from dotenv import load_dotenv
import os

load_dotenv()

CLIENT_ID     = os.environ["ZOHO_CLIENT_ID"]
CLIENT_SECRET = os.environ["ZOHO_CLIENT_SECRET"]
AUTH_URL      = os.environ.get("ZOHO_AUTH_URL", "https://accounts.zoho.com/oauth/v2/token")

if len(sys.argv) < 2:
    print("Usage: python3 get_token.py <authorization_code>")
    sys.exit(1)

code = sys.argv[1].strip()
print(f"Exchanging code: {code[:20]}...")

resp = requests.post(AUTH_URL, data={
    "code": code,
    "client_id": CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "grant_type": "authorization_code",
}, timeout=15)

data = resp.json()

if "refresh_token" not in data:
    print(f"\nFailed: {json.dumps(data, indent=2)}")
    sys.exit(1)

refresh_token = data["refresh_token"]
print(f"\nSuccess! Add to your .env:\nZOHO_REFRESH_TOKEN={refresh_token}")
