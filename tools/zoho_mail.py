import html as html_module
import re
import time
import requests
from loguru import logger
from config import Config


class ZohoMailClient:
    def __init__(self, config: Config):
        self.config = config
        self._access_token: str | None = None
        self._token_expiry: float = 0.0

    # ── Auth ─────────────────────────────────────────────────────────────────

    def _get_access_token(self) -> str:
        if self._access_token and time.time() < self._token_expiry - 60:
            return self._access_token
        logger.debug("Refreshing Zoho access token")
        resp = requests.post(
            self.config.zoho_auth_url,
            data={
                "refresh_token": self.config.zoho_refresh_token,
                "client_id": self.config.zoho_client_id,
                "client_secret": self.config.zoho_client_secret,
                "grant_type": "refresh_token",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if "access_token" not in data:
            raise RuntimeError(f"Zoho token refresh failed: {data}")
        self._access_token = data["access_token"]
        self._token_expiry = time.time() + int(data.get("expires_in", 3600))
        return self._access_token

    def _headers(self) -> dict:
        return {"Authorization": f"Zoho-oauthtoken {self._get_access_token()}"}

    def _base(self) -> str:
        return f"{self.config.zoho_mail_base_url}/accounts/{self.config.zoho_account_id}"

    # ── Inbox listing ─────────────────────────────────────────────────────────

    def list_inbox(self, limit: int = 50, start: int = 0) -> list[dict]:
        """Return up to *limit* inbox emails starting at *start*, newest first.
        Paginates automatically if limit > 200."""
        folder_id = self.config.zoho_inbox_folder_id
        if not folder_id:
            folder_id = self._get_inbox_folder_id()
        url = f"{self._base()}/messages/view"
        all_emails: list[dict] = []
        page_start = start
        page_size = 200  # Zoho max per request
        while len(all_emails) < limit:
            fetch = min(page_size, limit - len(all_emails))
            resp = requests.get(
                url,
                headers=self._headers(),
                params={"folderId": folder_id, "limit": fetch, "start": page_start},
                timeout=20,
            )
            resp.raise_for_status()
            batch = resp.json().get("data", [])
            if not batch:
                break
            all_emails.extend(batch)
            page_start += len(batch)
            if len(batch) < fetch:
                break  # no more emails
        logger.info("Listed {} email(s) from inbox", len(all_emails))
        return all_emails

    def list_inbox_since(self, since_ts: int, hard_cap: int = 500) -> list[dict]:
        """Return inbox emails with receivedTime newer than *since_ts* (epoch ms),
        newest first. Paginates until it passes since_ts or hits hard_cap — so a
        busy day never drops the oldest new email the way a fixed 'last N' can."""
        folder_id = self.config.zoho_inbox_folder_id or self._get_inbox_folder_id()
        url = f"{self._base()}/messages/view"
        out: list[dict] = []
        start = 0
        page = 200
        while len(out) < hard_cap:
            resp = requests.get(
                url, headers=self._headers(),
                params={"folderId": folder_id, "limit": min(page, hard_cap - len(out)), "start": start},
                timeout=20,
            )
            resp.raise_for_status()
            batch = resp.json().get("data", [])
            if not batch:
                break
            stop = False
            for e in batch:
                try:
                    rt = int(str(e.get("receivedTime", "0"))[:13] or 0)
                except ValueError:
                    rt = 0
                if since_ts and rt and rt <= since_ts:
                    stop = True
                    break
                out.append(e)
            start += len(batch)
            if stop or len(batch) < page:
                break
        logger.info("Listed {} email(s) newer than watermark", len(out))
        return out

    def _get_inbox_folder_id(self) -> str:
        resp = requests.get(
            f"{self._base()}/folders", headers=self._headers(), timeout=15
        )
        resp.raise_for_status()
        for f in resp.json().get("data", []):
            if "inbox" in f.get("folderName", "").lower():
                return f["folderId"]
        raise RuntimeError("Could not find Inbox folder")

    # ── Search ───────────────────────────────────────────────────────────────

    def search_emails(self, query: str, limit: int = 50) -> list[dict]:
        """Search all mail for *query* (subject, sender, body)."""
        url = f"{self._base()}/messages/search"
        resp = requests.get(
            url,
            headers=self._headers(),
            params={"searchKey": query, "limit": min(limit, 200)},
            timeout=20,
        )
        resp.raise_for_status()
        emails = resp.json().get("data", {}).get("list", [])
        logger.info("Search '{}' returned {} email(s)", query, len(emails))
        return emails

    # ── Email detail ─────────────────────────────────────────────────────────

    def get_email(self, message_id: str) -> dict:
        url = f"{self._base()}/messages/{message_id}/details"
        resp = requests.get(url, headers=self._headers(), timeout=15)
        resp.raise_for_status()
        return resp.json().get("data", {})

    def get_message_text(self, message_id: str, folder_id: str = "") -> str:
        """Return the email body (full chain) as plain text, HTML tags stripped.
        Used to scan a thread for payment-hold / dispute signals."""
        if not folder_id:
            folder_id = self.config.zoho_inbox_folder_id or self._get_inbox_folder_id()
        try:
            r = requests.get(
                f"{self._base()}/folders/{folder_id}/messages/{message_id}/content",
                headers=self._headers(), timeout=15,
            )
            if not r.ok:
                return ""
            html = r.json().get("data", {}).get("content", "") or ""
        except Exception as exc:  # noqa: BLE001 — body is best-effort
            logger.debug("Could not fetch body for {}: {}", message_id, exc)
            return ""
        # Strip tags + unescape entities → plain text
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.I | re.S)
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
        text = re.sub(r"</(p|div|tr|li|h[1-6])>", "\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html_module.unescape(text)
        return re.sub(r"[ \t]+", " ", text).strip()

    # ── Attachments ──────────────────────────────────────────────────────────

    def list_attachments(self, message_id: str, folder_id: str = "") -> list[dict]:
        """Return PDF attachments for a message.
        Uses /folders/{folderId}/messages/{msgId}/attachmentinfo endpoint."""
        if not folder_id:
            folder_id = self.config.zoho_inbox_folder_id or self._get_inbox_folder_id()
        url = f"{self._base()}/folders/{folder_id}/messages/{message_id}/attachmentinfo"
        resp = requests.get(url, headers=self._headers(), timeout=15)
        resp.raise_for_status()
        all_att = resp.json().get("data", {}).get("attachments", [])
        pdfs = [
            a for a in all_att
            if str(a.get("attachmentName", "")).lower().endswith(".pdf")
        ]
        logger.debug("message {} → {} PDF attachment(s)", message_id, len(pdfs))
        return pdfs

    def download_attachment(
        self, message_id: str, attachment_id: str, folder_id: str = ""
    ) -> bytes:
        """Download a specific attachment.
        Uses /folders/{folderId}/messages/{msgId}/attachments/{attId} endpoint."""
        if not folder_id:
            folder_id = self.config.zoho_inbox_folder_id or self._get_inbox_folder_id()
        url = f"{self._base()}/folders/{folder_id}/messages/{message_id}/attachments/{attachment_id}"
        resp = requests.get(url, headers=self._headers(), timeout=60)
        resp.raise_for_status()
        logger.debug("Downloaded attachment {} ({} bytes)", attachment_id, len(resp.content))
        return resp.content

    def dispatch_notified(self, email: dict) -> bool:
        """Return True if dispatch@ already appears in To, CC, or From of this email."""
        fields = " ".join([
            email.get("fromAddress", ""),
            email.get("toAddress", ""),
            email.get("ccAddress", ""),
        ]).lower()
        return "dispatch@demologistics.com" in fields

    def _upload_attachment(self, name: str, data: bytes) -> dict | None:
        """Upload a file to the account's draft store and return its
        {storeName, attachmentName, attachmentPath} reference for use in a
        send/reply body. Returns None on failure."""
        up = requests.post(
            f"{self._base()}/messages/attachments",
            headers=self._headers(),
            params={"fileName": name},
            data=data, timeout=60,
        )
        if not up.ok:
            logger.warning("Attachment upload failed for {}: {} {}", name, up.status_code, up.text[:120])
            return None
        d = up.json().get("data")
        d = d[0] if isinstance(d, list) else d
        if not d or "storeName" not in d:
            return None
        return {
            "storeName":      d["storeName"],
            "attachmentName": d.get("attachmentName", name),
            "attachmentPath": d["attachmentPath"],
        }

    def forward_threaded(
        self,
        message_id: str,
        to_address: str,
        subject: str = "",
        note_html: str = "",
        folder_id: str = "",
        from_address: str = "accounts@demologistics.com",
    ) -> str | None:
        """Forward an email so it lands as a REAL threaded chain in the recipient's
        mailbox (quoted original + all attachments), not a flat fresh compose.

        Zoho's REST API has no working ``forward`` action — POSTing action=forward
        to /messages/{id} returns 500. The ``reply`` action, however, threads the
        message (sets In-Reply-To / References) AND auto-quotes the original body
        below our note — which is exactly the chain we want. We override
        ``toAddress`` to the forward target (so it does NOT go back to the original
        sender) and re-upload the original's attachments, because the reply action
        drops them. Zoho saves the result to Sent automatically.

        Requires ZohoMail.messages.CREATE scope. Returns the new message id on
        success, else None.
        """
        if not folder_id:
            folder_id = self.config.zoho_inbox_folder_id or self._get_inbox_folder_id()

        # ── Re-attach every original attachment (ALL types, not just PDFs) ──
        # The reply action strips attachments, so we carry them ourselves.
        uploaded: list[dict] = []
        try:
            r = requests.get(
                f"{self._base()}/folders/{folder_id}/messages/{message_id}/attachmentinfo",
                headers=self._headers(), timeout=15,
            )
            att_list = r.json().get("data", {}).get("attachments", []) if r.ok else []
        except Exception as exc:  # noqa: BLE001 — attachments are best-effort
            logger.warning("Could not list attachments for forward {}: {}", message_id, exc)
            att_list = []

        for att in att_list:
            att_id   = att.get("attachmentId") or att.get("id", "")
            att_name = att.get("attachmentName", "attachment")
            try:
                data = self.download_attachment(message_id, att_id, folder_id)
                ref = self._upload_attachment(att_name, data)
                if ref:
                    uploaded.append(ref)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not carry attachment {} on forward: {}", att_name, exc)

        # ── Threaded send via the reply action (auto-quotes the original) ──
        payload: dict = {
            "fromAddress": from_address,
            "toAddress":   to_address,
            "subject":     subject or "Fwd:",
            "action":      "reply",       # only working threading action; quotes original
            "mailFormat":  "html",
            "content":     note_html or "",
        }
        if uploaded:
            payload["attachments"] = uploaded

        resp = requests.post(
            f"{self._base()}/messages/{message_id}",
            headers=self._headers(),
            json=payload, timeout=60,
        )
        if resp.status_code == 200:
            new_id = resp.json().get("data", {}).get("messageId")
            logger.debug(
                "Forwarded (threaded) msg {} → {} as {} with {} attachment(s)",
                message_id, to_address, new_id, len(uploaded),
            )
            return new_id or "ok"
        logger.warning("Threaded forward failed: {} {}", resp.status_code, resp.text[:200])
        return None

    def _upload_attachment(self, filename: str, content: bytes,
                           content_type: str = "application/pdf") -> dict | None:
        """Upload a file to Zoho and return its {storeName, attachmentPath,
        attachmentName} reference for attaching to a message."""
        resp = requests.post(
            f"{self._base()}/messages/attachments",
            headers=self._headers(),
            params={"uploadType": "multipart", "fileName": filename},
            files={"attach": (filename, content, content_type)},
            timeout=60,
        )
        if resp.status_code != 200:
            logger.warning("attachment upload failed: {} {}", resp.status_code, resp.text[:200])
            return None
        data = resp.json().get("data")
        return data[0] if isinstance(data, list) else data

    def send_email(self, to_address: str, subject: str, html: str,
                   from_address: str = "accounts@demologistics.com",
                   attachments: list[tuple[str, bytes]] | None = None,
                   cc: str = "") -> bool:
        """Send an HTML email, optionally with file attachments and CC recipients.
        `attachments` = [(filename, bytes), ...] — uploaded first, then referenced.
        `cc` = comma-separated CC addresses (Zoho `ccAddress`)."""
        payload = {"fromAddress": from_address, "toAddress": to_address,
                   "subject": subject, "content": html, "mailFormat": "html"}
        if cc:
            payload["ccAddress"] = cc
        if attachments:
            refs = []
            for fname, content in attachments:
                ref = self._upload_attachment(fname, content)
                if ref:
                    refs.append({k: ref[k] for k in ("storeName", "attachmentPath", "attachmentName")
                                 if k in ref})
            if refs:
                payload["attachments"] = refs
        resp = requests.post(
            f"{self._base()}/messages", headers=self._headers(), json=payload, timeout=60,
        )
        ok = resp.status_code == 200
        if not ok:
            logger.warning("send_email failed: {} {}", resp.status_code, resp.text[:200])
        return ok

    def mark_as_read(self, message_id: str, folder_id: str = "") -> bool:
        """Mark a message as read. Requires ZohoMail.messages.UPDATE scope."""
        if not folder_id:
            folder_id = self.config.zoho_inbox_folder_id or self._get_inbox_folder_id()
        url = f"{self._base()}/updatemessage"
        resp = requests.put(
            url,
            headers=self._headers(),
            json={"mode": "markAsRead", "messageId": [message_id], "folderId": folder_id},
            timeout=15,
        )
        ok = resp.status_code == 200
        if ok:
            logger.debug("Marked message {} as read", message_id)
        else:
            logger.warning("Could not mark {} as read: {} {}", message_id, resp.status_code, resp.text[:100])
        return ok

    # ── Account discovery helper ──────────────────────────────────────────────

    def get_accounts(self) -> list[dict]:
        url = f"{self.config.zoho_mail_base_url}/accounts"
        resp = requests.get(url, headers=self._headers(), timeout=15)
        resp.raise_for_status()
        return resp.json().get("data", [])
