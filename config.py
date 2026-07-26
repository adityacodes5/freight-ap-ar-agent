import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    def __init__(self):
        # Anthropic
        self.anthropic_api_key = self._require("ANTHROPIC_API_KEY")

        # Zoho Mail
        self.zoho_client_id = self._require("ZOHO_CLIENT_ID")
        self.zoho_client_secret = self._require("ZOHO_CLIENT_SECRET")
        self.zoho_refresh_token = self._require("ZOHO_REFRESH_TOKEN")
        self.zoho_account_id = self._require("ZOHO_ACCOUNT_ID")
        self.zoho_mail_base_url = os.getenv(
            "ZOHO_MAIL_BASE_URL", "https://mail.zoho.com/api"
        )
        self.zoho_auth_url = os.getenv(
            "ZOHO_AUTH_URL", "https://accounts.zoho.com/oauth/v2/token"
        )

        # Google Sheets — locally we point at a JSON file; on hosts without a
        # filesystem (Render cron/web) pass the same JSON base64-encoded instead.
        self.google_service_account_file = os.getenv(
            "GOOGLE_SERVICE_ACCOUNT_FILE", "service-account.json"
        )
        self.google_service_account_b64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_B64", "")
        self.google_sheet_id = self._require("GOOGLE_SHEET_ID")
        self.sales_sheet_name = os.getenv("SALES_SHEET_NAME", "Sales")
        self.accounts_sheet_name = os.getenv("ACCOUNTS_SHEET_NAME", "Accounts")

        # Agent behaviour
        self.dispatch_email_sender = os.getenv("DISPATCH_EMAIL_SENDER", "")
        self.invoice_search_query = os.getenv("INVOICE_SEARCH_QUERY", "invoice")
        self.lookback_days = int(os.getenv("LOOKBACK_DAYS", "14"))
        self.zoho_inbox_folder_id = os.getenv("ZOHO_INBOX_FOLDER_ID", "")

    def _require(self, key: str) -> str:
        val = os.getenv(key)
        if not val:
            raise ValueError(f"Required environment variable '{key}' is not set")
        return val
