import base64
import json
import re

import gspread
from gspread.utils import rowcol_to_a1
from google.oauth2.service_account import Credentials
from loguru import logger
from config import Config

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

_FORMULA_LEAD = ("=", "+", "@", "\t", "\r", "\n")


def _defang_formula(value):
    """Neutralise spreadsheet formula injection (CWE-1236). A value Sheets would
    evaluate as a formula is prefixed with a single quote so it is stored as
    literal text. A leading '-' is only defanged when it is NOT a plain negative
    number, so legitimate amounts are unaffected."""
    s = "" if value is None else str(value)
    if not s:
        return value
    if s[0] in _FORMULA_LEAD:
        return "'" + s
    if s[0] == "-" and not re.fullmatch(r"-\d+(\.\d+)?", s):
        return "'" + s
    return value


class GoogleSheetsClient:
    def __init__(self, config: Config):
        self.config = config
        # Prefer a base64-encoded service account (hosted: Render) and fall back
        # to a JSON file on disk (local dev).
        if config.google_service_account_b64:
            info = json.loads(base64.b64decode(config.google_service_account_b64))
            creds = Credentials.from_service_account_info(info, scopes=_SCOPES)
        else:
            creds = Credentials.from_service_account_file(
                config.google_service_account_file, scopes=_SCOPES
            )
        self._gc = gspread.authorize(creds)
        self._spreadsheet = self._gc.open_by_key(config.google_sheet_id)
        logger.info(
            "Connected to Google Sheet: '{}'", self._spreadsheet.title
        )

    def _ws(self, sheet_name: str) -> gspread.Worksheet:
        return self._spreadsheet.worksheet(sheet_name)

    # ── Read ─────────────────────────────────────────────────────────────────

    def get_headers(self, sheet_name: str) -> list[str]:
        """Return the first row (column headers), deduplicating any duplicates."""
        raw = self._ws(sheet_name).row_values(1)
        seen: dict[str, int] = {}
        out = []
        for h in raw:
            if h in seen:
                seen[h] += 1
                out.append(f"{h}_{seen[h]}")
            else:
                seen[h] = 1
                out.append(h)
        return out

    def get_all_records(self, sheet_name: str) -> list[dict]:
        """Return all rows as list of {header: value} dicts.
        Handles duplicate header names by appending _2, _3 etc."""
        all_values = self._ws(sheet_name).get_all_values()
        if not all_values:
            return []
        raw_headers = all_values[0]
        # Deduplicate headers
        seen: dict[str, int] = {}
        headers = []
        for h in raw_headers:
            if h in seen:
                seen[h] += 1
                headers.append(f"{h}_{seen[h]}")
            else:
                seen[h] = 1
                headers.append(h)
        records = []
        for row in all_values[1:]:
            # Pad short rows
            padded = row + [""] * (len(headers) - len(row))
            records.append(dict(zip(headers, padded)))
        logger.debug("Read {} records from '{}'", len(records), sheet_name)
        return records

    def column_backgrounds(self, sheet_name: str, col_letter: str,
                           start_row: int, end_row: int) -> list[dict]:
        """Background colour of each cell in a single column over [start_row,
        end_row]. Returns one {red,green,blue} dict per row (empty {} = default
        white). Used to spot blue month-break separator rows on Payment Received."""
        rng = f"{sheet_name}!{col_letter}{start_row}:{col_letter}{end_row}"
        meta = self._spreadsheet.fetch_sheet_metadata({
            "ranges": [rng], "includeGridData": True,
            "fields": "sheets(data(rowData(values(effectiveFormat(backgroundColor)))))",
        })
        data = meta["sheets"][0]["data"][0]
        out: list[dict] = []
        for rd in data.get("rowData", []):
            vals = rd.get("values") or [{}]
            out.append((vals[0].get("effectiveFormat") or {}).get("backgroundColor", {}))
        return out

    def get_all_values(self, sheet_name: str) -> list[list]:
        """Return raw 2-D list including header row."""
        return self._ws(sheet_name).get_all_values()

    def find_row(self, sheet_name: str, column_index: int, value: str) -> int | None:
        """Return 1-based row number where column_index (1-based) == value, or None."""
        ws = self._ws(sheet_name)
        try:
            cell = ws.find(value, in_column=column_index)
            return cell.row
        except gspread.exceptions.CellNotFound:
            return None

    # ── Write ────────────────────────────────────────────────────────────────

    def update_cells_by_header(
        self,
        sheet_name: str,
        row: int,
        updates: dict[str, object],
    ) -> None:
        """Update arbitrary columns in *row* by matching header names.

        Args:
            sheet_name: Tab name.
            row: 1-based row number.
            updates: {column_header: new_value} mapping.
        """
        ws = self._ws(sheet_name)
        headers = self.get_headers(sheet_name)  # deduplicated — Factoring→Factoring_2 etc.
        cell_updates: list[gspread.Cell] = []
        for header, value in updates.items():
            try:
                col = headers.index(header) + 1  # 1-based
            except ValueError:
                logger.warning(
                    "Header '{}' not found in '{}' — skipping", header, sheet_name
                )
                continue
            cell_updates.append(
                gspread.Cell(row=row, col=col, value=_defang_formula(value))
            )
        if cell_updates:
            ws.update_cells(cell_updates, value_input_option="USER_ENTERED")
            logger.info(
                "Updated {} cell(s) in '{}' row {}", len(cell_updates), sheet_name, row
            )

    def append_row_by_header(
        self,
        sheet_name: str,
        row_data: dict[str, object],
    ) -> None:
        """Append a new row.  *row_data* is {column_header: value}."""
        ws = self._ws(sheet_name)
        headers = self.get_headers(sheet_name)  # deduplicated
        values = [""] * len(headers)
        for header, value in row_data.items():
            try:
                col = headers.index(header)  # 0-based for list
                values[col] = _defang_formula(value)
            except ValueError:
                logger.warning("Header '{}' not found — skipping", header)
        ws.append_row(values, value_input_option="USER_ENTERED")
        logger.info("Appended new row to '{}'", sheet_name)

    def update_cell(
        self, sheet_name: str, row: int, col: int, value: object
    ) -> None:
        self._ws(sheet_name).update_cell(row, col, value)

    def bold_cell_by_header(self, sheet_name: str, row: int, header: str) -> None:
        """Bold a specific cell identified by its column header name."""
        ws = self._ws(sheet_name)
        headers = self.get_headers(sheet_name)
        try:
            col = headers.index(header) + 1
        except ValueError:
            logger.warning("Header '{}' not found — cannot bold", header)
            return
        cell_addr = rowcol_to_a1(row, col)
        ws.format(cell_addr, {"textFormat": {"bold": True}})
        logger.debug("Bolded {} in '{}' row {}", cell_addr, sheet_name, row)

    def highlight_row_orange(self, sheet_name: str, row: int) -> None:
        """Orange row = invoice correct, but sheet pre-entry needs fixing."""
        ws = self._ws(sheet_name)
        last_col = len(ws.row_values(1))
        end_col_letter = gspread.utils.rowcol_to_a1(row, last_col).rstrip("0123456789")
        ws.format(f"A{row}:{end_col_letter}{row}", {
            "backgroundColor": {"red": 1.0, "green": 0.65, "blue": 0.0}
        })
        logger.info("Highlighted row {} orange in '{}'", row, sheet_name)

    def clear_row_highlight(self, sheet_name: str, row: int) -> None:
        """Reset a row's background to white (clears red/orange flags)."""
        ws = self._ws(sheet_name)
        last_col = len(ws.row_values(1))
        end_col_letter = gspread.utils.rowcol_to_a1(row, last_col).rstrip("0123456789")
        ws.format(f"A{row}:{end_col_letter}{row}", {
            "backgroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}
        })
        logger.info("Cleared highlight on row {} in '{}'", row, sheet_name)

    def highlight_row_red(self, sheet_name: str, row: int) -> None:
        """Fill the entire row background red to flag a discrepancy."""
        ws = self._ws(sheet_name)
        last_col = len(ws.row_values(1))
        end_col_letter = gspread.utils.rowcol_to_a1(row, last_col).rstrip("0123456789")
        cell_range = f"A{row}:{end_col_letter}{row}"
        ws.format(cell_range, {
            "backgroundColor": {"red": 1.0, "green": 0.0, "blue": 0.0}
        })
        logger.info("Highlighted row {} red in '{}'", row, sheet_name)

    def highlight_row_magenta(self, sheet_name: str, row: int) -> None:
        """Fill the entire row background magenta to flag a discrepancy.

        The AI uses MAGENTA (not red) for everything it flags — the team reserves
        red for their own manual meaning, so agent flags must be visually distinct."""
        ws = self._ws(sheet_name)
        last_col = len(ws.row_values(1))
        end_col_letter = gspread.utils.rowcol_to_a1(row, last_col).rstrip("0123456789")
        cell_range = f"A{row}:{end_col_letter}{row}"
        ws.format(cell_range, {
            "backgroundColor": {"red": 1.0, "green": 0.0, "blue": 1.0}
        })
        logger.info("Highlighted row {} magenta in '{}'", row, sheet_name)
