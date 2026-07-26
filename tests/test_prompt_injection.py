"""Tests for spreadsheet formula/CSV injection defence (V-001 / V-002, CWE-1236).

A carrier-emailed invoice field that begins with a formula lead character
(`=`, `+`, `-`, `@`, tab/newline) is evaluated live by Google Sheets when
`value_input_option="USER_ENTERED"`. `_defang_formula` prefixes such values
with a single quote so Sheets stores them as literal text, while leaving
legitimate amounts and plain strings untouched.

No network / no Google. Run: pytest
"""
import pytest

from tools.sheets import _defang_formula, GoogleSheetsClient


# ── Pure function: values that MUST be defanged ──────────────────────────────

@pytest.mark.parametrize("evil", [
    '=IMAGE("http://x")',
    '=IMPORTXML("http://evil/x","//a")',
    "+1",
    "@cmd",
    "\tinjected",
    "\rinjected",
    "\ninjected",
    "-abc",
    "-",
    "-5+5",
    "=1+1",
])
def test_defangs_formula_leads(evil):
    out = _defang_formula(evil)
    assert out.startswith("'"), f"{evil!r} should have been defanged"
    assert out == "'" + evil


# ── Pure function: values that MUST be left unchanged ────────────────────────

@pytest.mark.parametrize("safe", [
    "Perilla Trucking",
    "1900",
    "-50",
    "-50.00",
    "-0",
    "3.14",
    "0",
    "123 Main St, Suite 4",
    "load#4821",
])
def test_leaves_safe_values_unchanged(safe):
    assert _defang_formula(safe) == safe


def test_empty_and_none_unchanged():
    assert _defang_formula("") == ""
    assert _defang_formula(None) is None


def test_negative_number_is_not_defanged_but_negative_expression_is():
    # A plain negative amount is a legitimate value.
    assert _defang_formula("-1234.56") == "-1234.56"
    # Anything else starting with '-' (a formula-ish expression) is neutralised.
    assert _defang_formula("-1+A1").startswith("'")


# ── Sink test: update_cells_by_header persists a defanged value ──────────────

class _FakeWorksheet:
    """Captures the Cells handed to update_cells so we can assert on values."""
    def __init__(self, headers):
        self._headers = headers
        self.captured = None

    def row_values(self, _row):
        return self._headers

    def update_cells(self, cells, value_input_option=None):
        self.captured = cells


def test_update_cells_by_header_persists_defanged_value(monkeypatch):
    fake = _FakeWorksheet(["Load no", "Factoring"])

    # Bypass __init__ (no Google auth) and stub the worksheet lookup.
    client = GoogleSheetsClient.__new__(GoogleSheetsClient)
    monkeypatch.setattr(client, "_ws", lambda name: fake)

    client.update_cells_by_header(
        "Accounts", 5, {"Factoring": '=IMAGE("http://evil/x")'},
    )

    assert fake.captured is not None
    cell = fake.captured[0]
    assert cell.value.startswith("'"), "injected formula must be stored as literal text"
    assert cell.value == '\'=IMAGE("http://evil/x")'
