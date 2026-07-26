"""Unit tests for the carrier-batch pure logic — the bits that have caused
live bugs: which emails to skip, carrier-name matching, load-number scanning,
amount parsing, and the AI JSON-extraction guard.

These run with no network / no Google / no Zoho. Run: pytest
"""
import pytest

from run_carrier_batch import (
    _is_non_invoice_email,
    _has_invoice_subject,
    _names_match,
    _normalize_name,
    _compact,
    _company_numbers,
    _find_load_in_text,
    parse_amount,
    _is_unreadable_invoice,
    _is_own_company,
    _norm_invoice_date,
    epoch_to_date,
    _ai_read,
    _is_noa_email,
)


class TestInvoiceDate:
    def test_norm_invoice_date_formats(self):
        assert _norm_invoice_date("06/23/2026") == "06/23/2026"
        assert _norm_invoice_date("2026-06-23") == "06/23/2026"
        assert _norm_invoice_date("June 23, 2026") == "06/23/2026"
        assert _norm_invoice_date("6/23/26") == "06/23/2026"

    def test_norm_invoice_date_empty_or_bad(self):
        assert _norm_invoice_date("") == ""
        assert _norm_invoice_date(None) == ""
        assert _norm_invoice_date("not a date") == ""

    def test_epoch_to_date_is_toronto_local_not_utc(self):
        # 2026-06-23 22:00 EDT == 2026-06-24 02:00 UTC. Toronto-local must stay 06/23.
        import datetime as _dt
        from zoneinfo import ZoneInfo
        ts = int(_dt.datetime(2026, 6, 23, 22, 0, tzinfo=ZoneInfo("America/Toronto")).timestamp())
        assert epoch_to_date(str(ts)) == "06/23/2026"


# ─────────────────────────────────────────────────────────────────────────────
# _has_invoice_subject — candidate selection (NOA / chatter dropped here)
# ─────────────────────────────────────────────────────────────────────────────
class TestHasInvoiceSubject:
    @pytest.mark.parametrize("subject", [
        "Invoice 33542 Load# 5568",
        "OUR INVOICE I282592 & POD 5633",
        "5651",          # pure load ref
        "INV4162",       # invoice ref
    ])
    def test_invoice_subjects_are_candidates(self, subject):
        assert _has_invoice_subject(subject) is True

    @pytest.mark.parametrize("subject", [
        "NOA for Royal Gill MC# 1327132",   # notice of assignment, not an invoice
        "Out of office",
        "Re: thanks",
    ])
    def test_non_invoice_subjects_are_not_candidates(self, subject):
        assert _has_invoice_subject(subject) is False


# ─────────────────────────────────────────────────────────────────────────────
# _is_non_invoice_email — skip reminders/admin, keep anything invoice-ish
# ─────────────────────────────────────────────────────────────────────────────
class TestIsNonInvoiceEmail:
    @pytest.mark.parametrize("subject", [
        "Statement of Account from Galaxy Freightline Inc",
        "Account Statement - June",
        "Payment Reminder",
        "Past Due Reminder for your account",
        "Aging Report Q2",
    ])
    def test_definitive_reminders_are_skipped_even_with_invoice_words(self, subject):
        assert _is_non_invoice_email(subject, []) is True

    @pytest.mark.parametrize("subject", [
        "Payment Inquiry",
        "Payment status",
        "Following up on payment",
        "Void Cheque",
        "Direct Deposit form",
        "Bank letter",
    ])
    def test_pure_admin_emails_are_skipped(self, subject):
        assert _is_non_invoice_email(subject, []) is True

    @pytest.mark.parametrize("subject", [
        "Invoice 33542 Load# 5568",
        "Debtor Statements - FOCUS FREIGHTLINES",   # JD Factors — real invoices
        "Re: URGENT: VOID CHECK REQUIRED- invoice (load no 5512)",  # has invoice
        "OUR INVOICE I282592 & POD 5633",
        "Carrier Invoice - Royal Gill",
        "Factoring NOA + invoice 7452",
    ])
    def test_invoice_emails_are_not_skipped(self, subject):
        assert _is_non_invoice_email(subject, []) is False

    def test_attachment_only_void_cheque_is_skipped(self):
        assert _is_non_invoice_email("FW: documents", ["void cheque.pdf"]) is True

    def test_attachment_invoice_overrides_skip_attachment(self):
        # has both a void cheque AND an invoice attachment -> keep
        assert _is_non_invoice_email(
            "FW: documents", ["void cheque.pdf", "invoice_5568.pdf"]) is False


# ─────────────────────────────────────────────────────────────────────────────
# _is_noa_email — factoring Notice of Assignment / release letters are NOT invoices
# ─────────────────────────────────────────────────────────────────────────────
class TestIsNoaEmail:
    @pytest.mark.parametrize("subject", [
        "RTS Financial NOA: SPOT EDGE TRUCKING INC MC# 1153133",
        "Notice of Assignment - ACME Carriers LLC",
        "Letter of Release for XYZ Transport",
        "Assignment of Accounts — New Carrier Setup",
    ])
    def test_noa_subjects_are_skipped(self, subject):
        assert _is_noa_email(subject, []) is True

    def test_noa_on_attachment_is_skipped(self):
        assert _is_noa_email("FW: documents", ["Notice of Assignment.pdf"]) is True

    @pytest.mark.parametrize("subject", [
        "Invoice + NOA for load 5651",          # real invoice rides along
        "Freight invoice 4162 with NOA attached",
    ])
    def test_noa_with_invoice_signal_is_not_skipped(self, subject):
        assert _is_noa_email(subject, []) is False

    @pytest.mark.parametrize("subject", [
        "NOAH TRANSPORT INC invoice 5560",       # 'noa' inside a word, plus invoice
        "Rate confirmation load 5651",
    ])
    def test_non_noa_subjects_are_not_skipped(self, subject):
        assert _is_noa_email(subject, []) is False

    def test_noa_attachment_yields_to_invoice_attachment(self):
        # a real invoice PDF present alongside a NOA -> process, don't skip
        assert _is_noa_email(
            "FW: paperwork", ["Notice of Assignment.pdf", "invoice_5568.pdf"]) is False


# ─────────────────────────────────────────────────────────────────────────────
# _names_match — same company despite suffix/DBA/order/spacing/spelling
# ─────────────────────────────────────────────────────────────────────────────
class TestNamesMatch:
    @pytest.mark.parametrize("a,b", [
        ("Galaxy Freight Line", "GALAXY FREIGHTLINE INC"),
        ("Flash Transport dba Canada Inc", "Flash Transport"),
        ("P.A. Trucking and Transportation o/b 1271902 Alberta Inc.",
         "1271902 ALBERTA INC DBA PA TRUCKING"),
        ("Royal Gill Freight Services Inc", "Royal Gill Freight Services Inc."),
        ("FOCUS FREIGHTLINES (2687654 ON Inc)", "2687654 Ontario Inc"),
        ("Munaf Logistics Inc.", "MUNAF LOGISTICS"),
        ("KIRA FREIGHT INC", "Kira Freight Inc."),
    ])
    def test_same_company_matches(self, a, b):
        assert _names_match(a, b) is True

    @pytest.mark.parametrize("a,b", [
        ("Galaxy Freightline Inc", "SecondCo Freight Inc"),
        ("ABC Trucking Inc", "XYZ Cartage Ltd"),
        ("Flash Transport", "Flesh Logistics"),
        ("1271902 Alberta Inc", "9988776 Ontario Inc"),
        ("Munaf Logistics", "Kira Freight"),
    ])
    def test_different_companies_do_not_match(self, a, b):
        assert _names_match(a, b) is False

    def test_empty_never_matches(self):
        assert _names_match("", "Anything Inc") is False
        assert _names_match("Anything Inc", "") is False

    def test_shared_registration_number_short_circuits(self):
        # Totally different trade names but same legal entity number -> match
        assert _names_match("1271902 Alberta", "PA Trucking 1271902") is True


# ─────────────────────────────────────────────────────────────────────────────
# _is_unreadable_invoice — the 5555 class: nothing usable was read, so don't
# paint the real load row red / flag a bogus currency discrepancy.
# ─────────────────────────────────────────────────────────────────────────────
class TestIsUnreadableInvoice:
    def test_bare_payment_request_is_unreadable(self):
        # No invoice #, $0, no line-haul (model only guessed a currency).
        assert _is_unreadable_invoice("", 0.0, None) is True
        assert _is_unreadable_invoice("", 0.0, "") is True
        assert _is_unreadable_invoice("", 0.0, "0.00") is True

    def test_real_invoice_is_not_unreadable(self):
        assert _is_unreadable_invoice("002426", 3000.0, "3000.00") is False

    def test_amount_only_is_readable(self):
        # Real invoice we read an amount off but couldn't capture the number ->
        # still a real invoice (handled by the 'invoice # not captured' path).
        assert _is_unreadable_invoice("", 1900.0, None) is False

    def test_line_haul_only_is_readable(self):
        assert _is_unreadable_invoice("", 0.0, "1500.00") is False

    def test_number_only_no_amount_is_unreadable(self):
        # A number with NO dollar amount is NOT a readable invoice — this is the
        # 5683 class, where a Bill-of-Lading number got mistaken for the invoice #.
        # A real carrier invoice always owes a positive amount.
        assert _is_unreadable_invoice("04000000000382388", 0.0, None) is True
        assert _is_unreadable_invoice("INV-77", 0.0, None) is True


# ─────────────────────────────────────────────────────────────────────────────
# _is_own_company — wrong-lane guard: a shipper invoice (ours) that lands in the
# carrier batch names Demo as the "carrier" (the 5714 / Interfor-reply class).
# ─────────────────────────────────────────────────────────────────────────────
class TestIsOwnCompany:
    @pytest.mark.parametrize("name", [
        "Demo Logistics Inc",
        "DEMO LOGISTICS INC.",
        "Demo Logistics",
        "demo  logistics inc",
    ])
    def test_demo_is_own_company(self, name):
        assert _is_own_company(name) is True

    @pytest.mark.parametrize("name", [
        "WEST COAST FREIGHTLINE LLC",
        "1271902 ALBERTA INC DBA PA TRUCKING",
        "Galaxy Freightline Inc",
        "",
        "Demo Transport Solutions",  # different company, no "logistics"
    ])
    def test_real_carriers_are_not_own_company(self, name):
        assert _is_own_company(name) is False


class TestNameHelpers:
    def test_normalize_strips_suffix_and_punct(self):
        assert _normalize_name("Royal Gill Freight Services Inc.") == "royal gill freight services"

    def test_normalize_strips_dba(self):
        assert "dba" not in _normalize_name("Flash Transport DBA Canada Inc")

    def test_compact_removes_spaces(self):
        assert _compact("freight line") == _compact("freightline") == "freightline"

    def test_company_numbers_extracts_registration(self):
        assert _company_numbers("1271902 ALBERTA INC DBA PA TRUCKING") == {"1271902"}

    def test_company_numbers_ignores_short_numbers(self):
        assert _company_numbers("Load 5568 Invoice 33542") == set()


# ─────────────────────────────────────────────────────────────────────────────
# _find_load_in_text — single match only, never a wrong guess
# ─────────────────────────────────────────────────────────────────────────────
class TestFindLoadInText:
    LOADS = {"5568": 1, "5633": 2, "5664": 3}

    def test_single_match_returns_it(self):
        assert _find_load_in_text("Re: load 5568 paid", self.LOADS) == "5568"

    def test_ambiguous_returns_empty(self):
        assert _find_load_in_text("loads 5568 and 5633", self.LOADS) == ""

    def test_no_match_returns_empty(self):
        assert _find_load_in_text("invoice 99999", self.LOADS) == ""

    def test_empty_text_returns_empty(self):
        assert _find_load_in_text("", self.LOADS) == ""


# ─────────────────────────────────────────────────────────────────────────────
# parse_amount — money strings to float, robust to junk
# ─────────────────────────────────────────────────────────────────────────────
class TestParseAmount:
    @pytest.mark.parametrize("val,expected", [
        ("$1,450.00", 1450.0),
        ("1450", 1450.0),
        ("  $3,000 ", 3000.0),
        (1500, 1500.0),
        ("", 0.0),
        ("n/a", 0.0),
        (None, 0.0),
    ])
    def test_parse_amount(self, val, expected):
        assert parse_amount(val) == expected


# ─────────────────────────────────────────────────────────────────────────────
# _ai_read — JSON parsing guard (the multi-invoice list crash)
# ─────────────────────────────────────────────────────────────────────────────
class _FakeResp:
    def __init__(self, text):
        self.content = [type("B", (), {"text": text})()]


class _FakeAI:
    """Minimal stand-in for the Anthropic client: returns a canned response."""
    def __init__(self, text):
        self._text = text
        self.messages = self

    def create(self, **kwargs):
        return _FakeResp(self._text)


class TestAiRead:
    """_ai_read now returns a LIST of invoice dicts ([] on failure), so a
    multi-invoice document keeps every invoice."""
    def test_plain_dict_wrapped_in_list(self):
        ai = _FakeAI('{"load_number": "5568", "carrier_amount": 350.0}')
        out = _ai_read(ai, "prompt")
        assert [i["load_number"] for i in out] == ["5568"]

    def test_code_fenced_json_is_stripped(self):
        ai = _FakeAI('```json\n{"load_number": "5664"}\n```')
        assert _ai_read(ai, "prompt")[0]["load_number"] == "5664"

    def test_json_list_keeps_all_invoices(self):
        # multi-invoice PDF returning a list — ALL invoices are now kept
        ai = _FakeAI('[{"load_number": "5679"}, {"load_number": "5680"}]')
        out = _ai_read(ai, "prompt")
        assert [i["load_number"] for i in out] == ["5679", "5680"]

    def test_invoices_key_wrapper_is_unwrapped(self):
        ai = _FakeAI('{"invoices": [{"load_number": "1"}, {"load_number": "2"}]}')
        assert len(_ai_read(ai, "prompt")) == 2

    def test_non_dict_returns_empty(self):
        ai = _FakeAI('"just a string"')
        assert _ai_read(ai, "prompt") == []

    def test_garbage_returns_empty(self):
        ai = _FakeAI("not json at all")
        assert _ai_read(ai, "prompt") == []


class TestReadInvoiceMultiInvoice:
    """_read_invoice_pdf returns EVERY invoice — and old single-dict cache entries
    still work (wrapped into a one-item list)."""
    def test_cached_list_returns_all(self, monkeypatch):
        import run_carrier_batch as rb
        monkeypatch.setattr(rb.invoice_state, "get_cached_extraction",
                            lambda *a, **k: [{"load_number": "1"}, {"load_number": "2"}])
        att = {"attachmentId": "x", "attachmentName": "inv.pdf"}
        invoices, err, _ = rb._read_invoice_pdf(None, None, "msg", att, None)
        assert err is None and [i["load_number"] for i in invoices] == ["1", "2"]

    def test_cached_legacy_dict_is_wrapped(self, monkeypatch):
        import run_carrier_batch as rb
        monkeypatch.setattr(rb.invoice_state, "get_cached_extraction",
                            lambda *a, **k: {"load_number": "9"})  # old cache format
        att = {"attachmentId": "x", "attachmentName": "inv.pdf"}
        invoices, err, _ = rb._read_invoice_pdf(None, None, "msg", att, None)
        assert [i["load_number"] for i in invoices] == ["9"]


class TestLoadForwardGuard:
    """Per-load forward guard: a load forwards to dispatch at most once, even
    across multiple carrier emails (invoice + NOA + re-sends)."""

    @pytest.fixture
    def fresh_state(self, tmp_path, monkeypatch):
        import importlib
        import state
        importlib.reload(state)
        monkeypatch.setattr(state, "USE_DB", False)
        monkeypatch.setattr(state, "_FWDLOAD_FILE", str(tmp_path / "forwarded_loads.json"))
        return state

    def test_unforwarded_load_is_not_marked(self, fresh_state):
        assert fresh_state.is_load_forwarded("5693") is False

    def test_mark_then_is_forwarded(self, fresh_state):
        fresh_state.mark_load_forwarded("5693", "msg-1")
        assert fresh_state.is_load_forwarded("5693") is True

    def test_duplicate_emails_same_load_guarded(self, fresh_state):
        # first email forwards + marks; the NOA / re-send must see it as forwarded
        fresh_state.mark_load_forwarded("5693", "invoice-msg")
        assert fresh_state.is_load_forwarded("5693") is True   # NOA email
        assert fresh_state.is_load_forwarded("5693") is True   # re-send

    def test_load_no_normalized(self, fresh_state):
        fresh_state.mark_load_forwarded(5693)                  # int
        assert fresh_state.is_load_forwarded(" 5693 ") is True  # str w/ whitespace

    def test_blank_load_never_marked(self, fresh_state):
        fresh_state.mark_load_forwarded("")
        assert fresh_state.is_load_forwarded("") is False

    def test_different_loads_independent(self, fresh_state):
        fresh_state.mark_load_forwarded("5693")
        assert fresh_state.is_load_forwarded("5698") is False


class TestShipperSubjectLoad:
    """run_batch._subject_load — which dispatch subjects are processable and what
    load number they carry. The 'Invoice and POD' forwards used to be missed."""

    @staticmethod
    def _load(subj):
        from run_batch import _subject_load
        return _subject_load(subj)

    def test_classic_invoice_load(self):
        assert self._load("Invoice_5633 Load_5633") == "5633"

    def test_re_invoice(self):
        assert self._load("Re: Invoice 5660") == "5660"

    def test_invoice_and_pod(self):
        assert self._load("Fwd: Invoice and POD 5715") == "5715"

    def test_invoice_ampersand_pod(self):
        assert self._load("Fwd: Invoice & POD 5715") == "5715"

    def test_pod_dotted(self):
        assert self._load("Invoice and P.O.D 5720") == "5720"

    def test_load_takes_load_group_when_both(self):
        # when both Invoice_ and Load_ present, the Load number wins
        assert self._load("Invoice_1234 Load_5678") == "5678"

    def test_non_invoice_subject_ignored(self):
        assert self._load("Payment Inquiry - REV Capital - Ticket 133761") == ""

    def test_pod_without_invoice_word_ignored(self):
        # POD alone (no 'invoice') isn't treated as an invoice email
        assert self._load("POD for 5715") == ""

    def test_no_number(self):
        assert self._load("Fwd: Invoice and POD") == ""
