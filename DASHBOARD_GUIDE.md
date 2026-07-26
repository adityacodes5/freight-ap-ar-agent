# Demo Invoice Dashboard — How to use it

A plain-English guide for the Demo team. No coding needed.

**Dashboard:** https://logistics-agent.vercel.app

---

## Signing in

1. Open the dashboard link.
2. Click **Sign in with Google** and pick your Demo Google account.
3. Only approved accounts can get in. If you see *"isn't authorized,"* ask Owner
   to add your email.

---

## What the agent does

It reads invoice emails and fills in the **DEMO Accounts 2026** Google Sheet for you:

- **Shipper invoices** (forwarded by dispatch) → fills the customer invoice details
  on the load's row.
- **Carrier invoices** (from carriers/factoring companies) → verifies the carrier
  and amount, and forwards clean ones to dispatch.

It never invents data — if something doesn't match, it flags it for a human
instead of guessing.

---

## The buttons

| Button | What it does |
|---|---|
| **Shipper Invoices** | Reads the latest shipper/dispatch invoice emails and updates the sheet. |
| **Carrier Invoices** | Reads the latest carrier invoice emails, verifies them, forwards clean ones to dispatch. |
| **Sync new loads** | Copies new loads from the India-team **SALES** sheet into the Accounts sheet (so invoices have a row to match). Shows a preview first — click **Add to sheet** to confirm. |

The **Emails** number sets how many recent emails to scan (default is fine).

It also runs **automatically twice a day** (around noon and 6 PM), so most of the
time you just check the dashboard rather than pressing buttons.

---

## Reading the results

After a run you'll see counts:

- **Verified** ✓ — matched and filled in. Nothing to do.
- **Discrepancy** ⚠ — the invoice doesn't match the sheet (name, amount, or
  currency). The row is highlighted **red** in the sheet. Needs a human look.
- **On Hold** ⛔ — the email thread suggests the payment shouldn't go through yet
  (e.g. a rate dispute). The agent did **not** touch the sheet and left the email
  **unread** for you to review.
- **Needs Review** ⚑ — the agent wouldn't enter it on its own. Most common reason:
  **the rate confirmation is missing** (a shipper invoice is only entered when
  *both* the dispatch-branded rate confirmation **and** the invoice are present and
  their numbers line up). Also covers "couldn't tell what this email is." Nothing
  is written to the sheet; the email is left **unread** for you.
- **No row** — the invoice is for a load that isn't in the sheet yet. Run
  **Sync new loads**, then re-run — it'll pick it up.
- **Already done** — was processed on a previous run. Skipped (no double work).
- **Errors** — couldn't read the file. Rare; usually a corrupt attachment.

---

## "Needs Review" list

Anything flagged (discrepancy / hold / error) shows up under **Needs Review** with
the reason. Click **details** to see invoice-vs-sheet values. Once you've handled
it (fixed the sheet, or confirmed it's fine), click **Mark done** to clear it.

You can also use **Fix by Chat** — type a correction in plain English, e.g.
*"load 5621 carrier amount should be 2100 and clear the flag."*

---

## Good habits

- Check the dashboard once or twice a day; act on the **Needs Review** list.
- A **red** row in the sheet = the agent flagged it; don't trust that row until
  someone reconciles it.
- **On Hold** emails are intentionally left unread — handle the dispute, then
  reprocess.
- The agent only changes invoice/amount fields. The **STATUS** column is yours —
  it only moves when a payment is made or dispatch says to hold.

---

*Questions or something looks off? Ping Owner.*
