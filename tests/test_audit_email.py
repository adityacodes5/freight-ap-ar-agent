"""Pure-function tests for the email completeness audit classifier."""
from audit_email import _classify, _looks_invoice


def test_looks_invoice_true_for_invoice_pdfs():
    assert _looks_invoice("INVOICE_2705.PDF")
    assert _looks_invoice("Fuentes_0084292 invoice.pdf")
    assert _looks_invoice("Inv 11943.pdf")


def test_looks_invoice_false_for_supporting_docs():
    # the docs that ride along with an invoice but aren't one
    assert not _looks_invoice("DIRECT DEPOSIT CANADA.PDF")
    assert not _looks_invoice("Load_Confirmation_5733.pdf")
    assert not _looks_invoice("POD For ORD16099.pdf")
    assert not _looks_invoice("CAM CAD Void Cheque Scotia Bank.pdf")
    assert not _looks_invoice("NOA_DRIVELOAD.PDF")
    assert not _looks_invoice("random.pdf")


def test_classify_junk_noa_and_statements():
    assert _classify("Notice of Assignment for STALLIONS", ["Noa.pdf"], "x@jdfactors.com") == "junk"
    assert _classify("NOA - ARC EXPRESS", ["Noa.pdf"], "paperwork@otr.com") == "junk"
    assert _classify("Debtor Statement", ["DebStmt.pdf"], "invoices@transwest.com") == "junk"
    assert _classify("VOID CHECK REQUIRED 5474", ["void.pdf"], "x@carrier.com") == "junk"


def test_classify_junk_when_no_invoice_signal():
    assert _classify("Hello there", ["photo.pdf"], "someone@x.com") == "junk"


def test_classify_carrier_vs_shipper():
    assert _classify("Transport Invoice #0084292 Ref#5758", ["inv.pdf"], "fst@fsttrans.com") == "carrier"
    assert _classify("Invoice_5775 Load_5775", ["Invoice_5775.pdf"],
                     "dispatch@demologistics.com") == "shipper"
