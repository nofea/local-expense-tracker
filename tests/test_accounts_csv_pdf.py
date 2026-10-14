"""Tests for accounts (§7.1, Appendix A item 8), CSV mappings (§3.2, item 11)
and password-protected PDFs (§3.2, item 12)."""

import os
import tempfile

# Never touch data/expenses.db: point the app at a throwaway DB before any
# core/web import reads the path (app_spec.md §11).
os.environ.setdefault(
    "EXPENSE_TRACKER_DB",
    os.path.join(tempfile.mkdtemp(prefix="expense-tests-"), "expenses.db"),
)

import hashlib
import re
import sqlite3
import struct
import unittest
import uuid
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import core.database as db
from core import csv_import, jobs, staging
from core.analytics import get_monthly_summary
from core.csv_import import CsvMapping
from core.ingestion import PasswordRequired, extract_pages_from_pdf
from core.models import ExtractedTransaction, StatementMetadata
from web.app import app


# ---------------------------------------------------------------------------
# A minimal PDF, optionally encrypted with the standard RC4-40 handler (R2),
# so password handling is tested against real pdfplumber behaviour.
# ---------------------------------------------------------------------------

_PAD = bytes.fromhex("28BF4E5E4E758A4164004E56FFFA01082E2E00B6D0683E802F0CA9FE6453697A")


def _rc4(key: bytes, data: bytes) -> bytes:
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) % 256
        s[i], s[j] = s[j], s[i]
    out = bytearray()
    i = j = 0
    for b in data:
        i = (i + 1) % 256
        j = (j + s[i]) % 256
        s[i], s[j] = s[j], s[i]
        out.append(b ^ s[(s[i] + s[j]) % 256])
    return bytes(out)


def make_pdf(lines: list[str], user_password: str | None = None) -> bytes:
    content = ("BT /F1 10 Tf 50 750 Td 12 TL " + " ".join(f"({l}) '" for l in lines) + " ET").encode("latin-1")
    file_id = hashlib.md5(uuid.uuid4().bytes).digest()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        None,  # content stream, below
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    if user_password is not None:
        pad = lambda pw: (pw.encode("latin-1") + _PAD)[:32]
        owner = _rc4(hashlib.md5(pad("owner")).digest()[:5], pad(user_password))
        perms = -4
        key = hashlib.md5(pad(user_password) + owner + struct.pack("<i", perms) + file_id).digest()[:5]
        content = _rc4(hashlib.md5(key + struct.pack("<i", 4)[:3] + b"\0\0").digest()[:10], content)
        objs.append(
            b"<< /Filter /Standard /V 1 /R 2 /O <%s> /U <%s> /P %d >>"
            % (owner.hex().encode(), _rc4(key, _PAD).hex().encode(), perms)
        )
    objs[3] = b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    trailer = b"/Size %d /Root 1 0 R /ID [<%s> <%s>]" % (len(objs) + 1, file_id.hex().encode(), file_id.hex().encode())
    if user_password is not None:
        trailer += b" /Encrypt 6 0 R"
    out += b"trailer\n<< " + trailer + b" >>\nstartxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


STATEMENT_LINES = ["01 Mar 2025 COFFEE HOUSE -150.00", "02 Mar 2025 SALARY ACME 90000.00"]


def _tx(date, desc, amount):
    return ExtractedTransaction(transaction_date=date, raw_description=desc, amount=amount)


class _TempDB(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "expenses.db"
        db.init_db(self.path)
        self.account = db.get_or_create_account(db_path=self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def q(self, sql, params=()):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def job(self, content: bytes, name: str, *, mapping=None, password=None):
        path = jobs.tmp_dir(self.path) / f"statement-{uuid.uuid4().hex}"
        path.write_bytes(content)
        sid = staging.create_import(
            account_id=self.account,
            file_name=name,
            file_sha256=uuid.uuid4().hex,
            model="llama3.1",
            extractor="llm" if mapping is None else "csv_mapping",
            db_path=self.path,
        )
        return jobs.Job(sid, path, name, "llama3.1", self.path, mapping=mapping, password=password)


# ---------------------------------------------------------------------------
# Item 11: deterministic CSV parsing
# ---------------------------------------------------------------------------

HDFC_STYLE = """HDFC BANK Ltd.,,,,,
Account No : 1234,,,,,
,,,,,
Date,Narration,Value Dt,Withdrawal Amt.,Deposit Amt.,Closing Balance
01/03/25,UPI-SWIGGY,01/03/25,450.00,,9550.00
02/03/25,SALARY ACME,02/03/25,,"90,000.00","99,550.00"
,,,,,
03/03/25,COFFEE HOUSE,03/03/25,150.00,,99400.00
not a date,TOTAL,,,,
"""


def _rows(text=HDFC_STYLE):
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8") as f:
        f.write(text)
    try:
        return csv_import.read_rows(f.name)
    finally:
        os.unlink(f.name)


def _hdfc_mapping(rows, **kw):
    fields = dict(
        header_signature=csv_import.header_signature(rows[3]), skip_rows=3, date_col="Date",
        date_format="%d/%m/%y", description_col="Narration", value_date_col="Value Dt",
        debit_col="Withdrawal Amt.", credit_col="Deposit Amt.",
    )
    fields.update(kw)
    return CsvMapping(**fields)


class TestCsvParsing(unittest.TestCase):
    rows = staticmethod(_rows)
    mapping = staticmethod(_hdfc_mapping)

    def test_detects_header_below_preamble(self):
        rows = self.rows()
        self.assertEqual(csv_import.detect_header(rows), 3)
        self.assertEqual(csv_import.detect_header(self.rows("Date,Desc,Amount\n2025-01-01,x,-1\n")), 0)

    def test_header_signature_ignores_case_space_and_trailing_cells(self):
        sig = csv_import.header_signature
        self.assertEqual(sig(["Date", "Narration", "Amount"]), sig([" date ", "NARRATION", "amount", "", ""]))
        self.assertNotEqual(sig(["Date", "Narration", "Amount"]), sig(["Date", "Amount", "Narration"]))

    def test_parse_amount(self):
        p = csv_import.parse_amount
        self.assertEqual(p("-1,234.50"), Decimal("-1234.50"))
        self.assertEqual(p("(1,234.50)"), Decimal("-1234.50"))
        self.assertEqual(p("₹ 99"), Decimal("99"))
        self.assertEqual(p("500.00 Dr"), Decimal("-500.00"))
        self.assertEqual(p("500.00 CR"), Decimal("500.00"))
        self.assertIsNone(p(""))
        self.assertIsNone(p("n/a"))

    def test_parse_date(self):
        self.assertEqual(csv_import.parse_date("31/03/25", "%d/%m/%y"), "2025-03-31")
        self.assertEqual(csv_import.parse_date("31/03/2025 10:15:00", "%d/%m/%Y"), "2025-03-31")
        self.assertEqual(csv_import.parse_date("31 Mar 2025", "%d %b %Y"), "2025-03-31")
        self.assertIsNone(csv_import.parse_date("31/02/2025", "%d/%m/%Y"))

    def test_guess_mapping_for_split_columns(self):
        rows = self.rows()
        guess = csv_import.guess_mapping(rows[3], rows[4:6])
        self.assertEqual(
            {k: guess[k] for k in ("date_col", "date_format", "value_date_col", "description_col",
                                   "amount_mode", "debit_col", "credit_col")},
            {"date_col": "Date", "date_format": "%d/%m/%y", "value_date_col": "Value Dt",
             "description_col": "Narration", "amount_mode": "split",
             "debit_col": "Withdrawal Amt.", "credit_col": "Deposit Amt."},
        )
        single = csv_import.guess_mapping(["Txn Date", "Description", "Amount (INR)"], [["2025-03-01", "x", "-1"]])
        self.assertEqual((single["date_col"], single["amount_col"], single["amount_mode"]),
                         ("Txn Date", "Amount (INR)", "single"))

    def test_parse_split_columns_keeps_bad_rows_for_review(self):
        rows = self.rows()
        parsed = csv_import.parse(rows, self.mapping(rows))
        self.assertEqual(
            [(r["transaction_date"], r["raw_description"], r["amount"]) for r in parsed],
            [
                ("2025-03-01", "UPI-SWIGGY", Decimal("-450.00")),
                ("2025-03-02", "SALARY ACME", Decimal("90000.00")),
                ("2025-03-03", "COFFEE HOUSE", Decimal("-150.00")),
                ("not a date", "TOTAL", None),  # blank line skipped; junk row kept for review to flag
            ],
        )
        self.assertEqual(parsed[0]["value_date"], "2025-03-01")

    def test_parse_single_column_with_negate(self):
        rows = self.rows("Date,Description,Amount\n2025-03-01,AMAZON,1200.00\n2025-03-05,PAYMENT THANK YOU,-5000\n")
        m = CsvMapping(header_signature="x", skip_rows=0, date_col="Date", date_format="%Y-%m-%d",
                       description_col="Description", amount_col="Amount", negate=True)
        self.assertEqual([r["amount"] for r in csv_import.parse(rows, m)], [Decimal("-1200.00"), Decimal("5000")])

    def test_validate(self):
        rows = self.rows()
        csv_import.validate(rows, self.mapping(rows))
        bad = [
            (dict(date_format="%Y-%m-%d"), "look like 2025-03-31"),
            (dict(date_col="Nope"), "no column named 'Nope'"),
            (dict(debit_col=None), "debit and a credit"),
            (dict(description_col=""), "date and description"),
            (dict(skip_rows=99), "past the end"),
            (dict(date_format="%s"), "from the list"),
        ]
        for kw, message in bad:
            with self.subTest(kw=kw), self.assertRaisesRegex(csv_import.CsvMappingError, message):
                csv_import.validate(rows, self.mapping(rows, **kw))

    def test_find_mapping_uses_each_mappings_skip_rows(self):
        rows = self.rows()
        m = self.mapping(rows)
        self.assertIs(csv_import.find_mapping(rows, [m]), m)
        self.assertIsNone(csv_import.find_mapping(rows, [self.mapping(rows, skip_rows=0)]))
        self.assertIsNone(csv_import.find_mapping(self.rows("Date,Amount\n"), [m]))


@patch("core.ingestion.extract_statement_metadata")
@patch("core.ingestion.extract_transactions")
class TestMappedCsvJob(_TempDB):
    def mapping(self):
        m = _hdfc_mapping(_rows())
        m.id = db.save_csv_mapping(self.account, m, self.path)
        return m

    def test_mapped_csv_is_staged_without_ollama(self, mock_tx, mock_meta):
        job = self.job(HDFC_STYLE.encode(), "mar.csv", mapping=self.mapping())
        jobs.run_job(job)

        mock_tx.assert_not_called()
        mock_meta.assert_not_called()
        self.assertFalse(job.file_path.exists())
        review = staging.get_review(job.statement_id, self.path)
        s = review.statement
        self.assertEqual(
            (s["status"], s["extractor"], s["model"], s["period_start"], s["period_end"], s["progress_done"]),
            ("review", "csv_mapping", None, "2025-03-01", "2025-03-03", 1),
        )
        self.assertEqual([r["amount_minor"] for r in review.rows], [-45000, 9000000, -15000, None])
        self.assertEqual(review.rows[-1]["issues"], ["bad_date", "zero_amount"])
        self.assertFalse(any("not_in_source" in r["issues"] for r in review.rows))

    def test_mapping_crud(self, *_):
        m = self.mapping()
        self.assertEqual([x.id for x in db.get_csv_mappings(self.account, self.path)], [m.id])
        m.negate = True
        self.assertEqual(db.save_csv_mapping(self.account, m, self.path), m.id)  # upsert
        self.assertTrue(db.get_csv_mappings(self.account, self.path)[0].negate)
        self.assertTrue(db.delete_csv_mapping(m.id, self.path))
        self.assertEqual(db.get_csv_mappings(db_path=self.path), [])


# ---------------------------------------------------------------------------
# Item 12: password-protected PDFs
# ---------------------------------------------------------------------------

class TestPdfPassword(unittest.TestCase):
    def write(self, data):
        f = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        f.write(data)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_plain_and_encrypted_pdfs(self):
        plain = self.write(make_pdf(STATEMENT_LINES))
        self.assertIn("COFFEE HOUSE", extract_pages_from_pdf(plain)[0])

        locked = self.write(make_pdf(STATEMENT_LINES, "s3cret"))
        with self.assertRaisesRegex(PasswordRequired, "password-protected"):
            extract_pages_from_pdf(locked)
        with self.assertRaisesRegex(PasswordRequired, "incorrect") as ctx:
            extract_pages_from_pdf(locked, "wrong-pw")
        self.assertNotIn("wrong-pw", str(ctx.exception))
        self.assertIn("SALARY ACME", extract_pages_from_pdf(locked, "s3cret")[0])


@patch("core.ingestion.extract_statement_metadata", return_value=StatementMetadata())
@patch("core.ingestion.extract_transactions", return_value=[_tx("2025-03-01", "COFFEE HOUSE", -150.0)])
class TestPasswordJobs(_TempDB):
    def status(self, sid):
        return staging.get_statement(sid, self.path)

    def test_needs_password_then_review(self, mock_tx, _meta):
        job = self.job(make_pdf(STATEMENT_LINES, "s3cret"), "locked.pdf")
        self.assertNotIn("s3cret", repr(job))
        jobs.run_job(job)
        s = self.status(job.statement_id)
        self.assertEqual((s["status"], s["error"]), ("needs_password", "This PDF is password-protected."))
        self.assertTrue(job.file_path.exists(), "the file waits for the password")
        mock_tx.assert_not_called()

        self.assertTrue(jobs.provide_password(job.statement_id, "wrong-pw"))
        jobs.wait_idle()
        s = self.status(job.statement_id)
        self.assertEqual((s["status"], s["error"]), ("needs_password", "The password is incorrect."))
        self.assertTrue(job.file_path.exists())

        self.assertTrue(jobs.provide_password(job.statement_id, "s3cret"))
        jobs.wait_idle()
        s = self.status(job.statement_id)
        self.assertEqual((s["status"], s["error"]), ("review", None))
        self.assertFalse(job.file_path.exists())
        self.assertFalse(jobs.provide_password(job.statement_id, "s3cret"))  # no longer held
        # The password is never stored (§9.4).
        dump = "\n".join(str(tuple(r)) for r in self.q("SELECT * FROM statements"))
        self.assertNotIn("s3cret", dump)
        self.assertNotIn("wrong-pw", dump)

    def test_password_given_at_upload(self, *_):
        job = self.job(make_pdf(STATEMENT_LINES, "s3cret"), "locked.pdf", password="s3cret")
        jobs.run_job(job)
        self.assertEqual(self.status(job.statement_id)["status"], "review")
        self.assertFalse(job.file_path.exists())

    def test_cancel_deletes_file(self, *_):
        job = self.job(make_pdf(STATEMENT_LINES, "s3cret"), "locked.pdf")
        jobs.run_job(job)
        self.assertTrue(jobs.cancel(job.statement_id))
        self.assertFalse(job.file_path.exists())
        self.assertEqual(self.status(job.statement_id)["status"], "discarded")
        self.assertFalse(jobs.cancel(job.statement_id))

    def test_restart_fails_waiting_job(self, *_):
        job = self.job(make_pdf(STATEMENT_LINES, "s3cret"), "locked.pdf")
        jobs.run_job(job)
        self.assertEqual(staging.recover_interrupted(self.path), 1)
        self.assertEqual(self.status(job.statement_id)["status"], "failed")
        jobs._held.pop(job.statement_id, None)


# ---------------------------------------------------------------------------
# Item 8: accounts
# ---------------------------------------------------------------------------

class TestAccounts(_TempDB):
    def test_crud_and_validation(self):
        card = db.create_account("Amex  Gold", "credit_card", " Amex ", db_path=self.path)
        rows = {a["id"]: a for a in db.list_accounts(self.path)}
        self.assertEqual((rows[card]["name"], rows[card]["kind"], rows[card]["institution"]),
                         ("Amex Gold", "credit_card", "Amex"))
        with self.assertRaisesRegex(db.AccountError, "already exists"):
            db.create_account("Amex Gold", db_path=self.path)
        with self.assertRaisesRegex(db.AccountError, "type"):
            db.create_account("X", "loan", db_path=self.path)
        with self.assertRaisesRegex(db.AccountError, "name"):
            db.create_account("  ", db_path=self.path)
        with self.assertRaisesRegex(db.AccountError, "already exists"):
            db.update_account(card, name="Default", kind="bank", db_path=self.path)
        db.update_account(card, name="Amex Platinum", kind="credit_card", db_path=self.path)
        self.assertIn("Amex Platinum", [a["name"] for a in db.list_accounts(self.path)])

    def test_delete_rules(self):
        acct = db.create_account("Spare", db_path=self.path)
        db.save_csv_mapping(acct, CsvMapping("sig", 0, "Date", "%Y-%m-%d", "Desc", amount_col="Amt"), self.path)
        db.upsert_rule(db.CategoryRule(pattern="X", category="Y", account_id=acct), self.path)
        with self.assertRaisesRegex(db.AccountError, "rule"):
            db.delete_account(acct, self.path)
        conn = sqlite3.connect(self.path)
        conn.execute("DELETE FROM category_rules")
        conn.commit()
        conn.close()
        db.delete_account(acct, self.path)
        self.assertEqual(db.get_csv_mappings(db_path=self.path), [])  # mappings go with it

        db.import_statement([{"transaction_date": "2025-03-01", "raw_description": "A", "amount": -1}],
                            account_id=self.account, file_name="a.csv", file_sha256="a", db_path=self.path)
        with self.assertRaisesRegex(db.AccountError, "import"):
            db.delete_account(self.account, self.path)
        summary = {a["id"]: a for a in db.list_accounts_summary(self.path)}
        self.assertEqual((summary[self.account]["statement_count"], summary[self.account]["transaction_count"]), (1, 1))


# ---------------------------------------------------------------------------
# Web: upload with accounts, CSV mapping form, PDF password, accounts page,
# rules options and account filters
# ---------------------------------------------------------------------------

def _hidden(html):
    return dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)">', html))


@patch("web.app.list_available_models", return_value=[])
@patch("web.app.is_ollama_available", return_value=False)
class TestWeb(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.account_name = f"Acct {uuid.uuid4().hex[:8]}"

    def csv_bytes(self, *rows):
        return ("Txn Date,Particulars,Debit,Credit\n" + "\n".join(rows) + f"\n,# {uuid.uuid4()},,\n").encode()

    def upload(self, content, name="mar.csv", **data):
        data.setdefault("new_account_name", self.account_name)
        res = self.client.post("/upload", files={"file": (name, content, "text/csv")}, data=data)
        self.assertEqual(res.status_code, 200)
        return res

    def account_id(self):
        return next(a["id"] for a in db.list_accounts() if a["name"] == self.account_name)

    def test_csv_mapping_flow_needs_no_ollama(self, *_):
        res = self.upload(self.csv_bytes("05/04/2026,TEA STALL,40.00,", "06/04/2026,REFUND,,40.00"))
        self.assertIn("How should mar.csv be read?", res.text)
        self.assertIn('<option value="%d/%m/%Y" selected>', res.text)  # guessed
        form = _hidden(res.text)
        self.assertEqual(int(form["account_id"]), self.account_id())
        held = jobs.tmp_dir() / form["token"]
        self.assertTrue(held.exists())

        # A wrong date format is explained, not saved.
        mapping = dict(form, date_col="Txn Date", description_col="Particulars", amount_mode="split",
                       debit_col="Debit", credit_col="Credit")
        res = self.client.post("/upload/csv-mapping", data=dict(mapping, date_format="%Y-%m-%d"))
        self.assertIn("look like 2025-03-31", res.text)
        self.assertEqual(db.get_csv_mappings(self.account_id()), [])

        res = self.client.post("/upload/csv-mapping", data=dict(mapping, date_format="%d/%m/%Y", action="save"))
        jobs.wait_idle()
        sid = int(re.search(r"/imports/(\d+)/status", res.text).group(1))
        self.assertFalse(held.exists())
        review = staging.get_review(sid)
        self.assertEqual((review.statement["status"], review.statement["account_name"]), ("review", self.account_name))
        self.assertEqual([r["amount_minor"] for r in review.rows], [-4000, 4000, None])

        # The next file with the same header is parsed straight away.
        res = self.upload(self.csv_bytes("07/04/2026,BUS,12.00,"))
        self.assertNotIn("How should", res.text)
        jobs.wait_idle()
        sid2 = int(re.search(r"/imports/(\d+)/status", res.text).group(1))
        self.assertEqual(staging.get_statement(sid2)["extractor"], "csv_mapping")
        self.assertIn(self.account_name, self.client.get("/accounts").text)

    def test_mapping_refresh_cancel_and_llm_choice(self, avail, _models):
        res = self.upload(b"Bank statement\n" + self.csv_bytes("05/04/2026,TEA,40.00,"))
        form = _hidden(res.text)
        self.assertIn('name="skip_rows" min="0" value="1"', res.text)  # preamble detected
        res = self.client.post("/upload/csv-mapping", data=dict(form, action="refresh", skip_rows="0"))
        self.assertIn("<th>Bank statement</th>", res.text)

        res = self.client.post("/upload/csv-mapping", data=dict(form, action="llm"))
        self.assertIn("Ollama is not running", res.text)  # offline: form stays, with the error
        self.assertTrue((jobs.tmp_dir() / form["token"]).exists())

        res = self.client.post("/upload/csv-mapping", data=dict(form, action="cancel"))
        self.assertIn("cancelled", res.text)
        self.assertFalse((jobs.tmp_dir() / form["token"]).exists())
        res = self.client.post("/upload/csv-mapping", data=dict(form, action="llm"))
        self.assertIn("expired", res.text)

    def test_mapping_token_cannot_name_other_paths(self, *_):
        for token in ("../expenses.db", "statement-x.pdf", "/etc/passwd", "statement-../../x.csv"):
            with self.subTest(token=token):
                res = self.client.post("/upload/csv-mapping", data={"token": token, "account_id": "1", "action": "cancel"})
                self.assertIn("expired", res.text)

    def test_pdf_password_flow(self, avail, _models):
        avail.return_value = True
        with patch("core.ingestion.extract_transactions", return_value=[_tx("2026-05-01", "COFFEE HOUSE", -150.0)]), \
             patch("core.ingestion.extract_statement_metadata", return_value=StatementMetadata()):
            pdf = make_pdf(STATEMENT_LINES, "s3cret")
            res = self.client.post("/upload", files={"file": ("locked.pdf", pdf, "application/pdf")},
                                   data={"new_account_name": self.account_name})
            jobs.wait_idle()
            sid = int(re.search(r"/imports/(\d+)/status", res.text).group(1))
            page = self.client.get(f"/imports/{sid}").text
            self.assertIn("needs a password", page)
            self.assertIn(f'hx-post="/imports/{sid}/password"', page)
            self.assertIn("Enter password", self.client.get("/imports").text)

            res = self.client.post(f"/imports/{sid}/password", data={"password": "s3cret"})
            jobs.wait_idle()
            self.assertIn("ready for review", self.client.get(f"/imports/{sid}/status").text)

    def test_upload_to_unknown_account_is_refused(self, avail, _models):
        avail.return_value = True
        with patch("web.app.jobs.submit") as submit:
            res = self.client.post("/upload", files={"file": ("a.pdf", b"%PDF", "application/pdf")},
                                   data={"account_id": "999999"})
        submit.assert_not_called()
        self.assertIn("no longer exists", res.text)

    def test_accounts_page_crud(self, *_):
        res = self.client.post("/accounts", data={"name": self.account_name, "kind": "credit_card"})
        self.assertIn("created", res.text)
        acct = self.account_id()
        res = self.client.post("/accounts", data={"name": self.account_name})
        self.assertIn("already exists", res.text)
        res = self.client.post(f"/accounts/{acct}", data={"name": self.account_name + " 2", "kind": "bank"})
        self.assertIn("Account saved", res.text)
        self.account_name += " 2"
        res = self.client.delete(f"/accounts/{acct}")
        self.assertIn("Account deleted", res.text)
        self.assertNotIn(self.account_name, self.client.get("/accounts").text)

    def test_rules_options_and_match_count(self, *_):
        acct = db.create_account(self.account_name)
        token = uuid.uuid4().hex[:10].upper()
        db.import_statement(
            [{"transaction_date": "2026-06-01", "raw_description": f"CARD PAYMENT {token}", "amount": 500}],
            account_id=acct, file_name="x.csv", file_sha256=uuid.uuid4().hex,
        )
        self.assertEqual(self.client.get("/rules/match-count", params={"pattern": token.lower()}).text,
                         "Matches 1 transaction")
        self.assertEqual(self.client.get("/rules/match-count", params={"pattern": token, "account_id": "1"}).text,
                         "Matches 0 transactions")
        res = self.client.post("/rules", data={"pattern": token, "category": "Card bill", "priority": "5",
                                               "mark_transfer": "true", "account_id": str(acct)})
        self.assertIn(self.account_name, res.text)
        rule = next(r for r in db.get_rules() if r.pattern == token)
        self.assertEqual((rule.priority, rule.mark_transfer, rule.account_id), (5, True, acct))
        tx = db.get_transactions(search=token)[0]
        self.assertEqual((tx.category, tx.is_transfer), ("Card bill", True))
        self.client.delete(f"/rules/{rule.id}")

    def test_account_filters(self, *_):
        acct = db.create_account(self.account_name)
        db.import_statement(
            [{"transaction_date": "2031-01-05", "raw_description": "ONLY HERE", "amount": -7}],
            account_id=acct, file_name="x.csv", file_sha256=uuid.uuid4().hex,
        )
        other = db.get_or_create_account()
        dash = self.client.get(f"/?year=2031&month=1&account_id={acct}").text
        self.assertIn("₹7", dash)
        self.assertIn(f"&account_id={acct}", dash)  # month links keep the filter
        self.assertNotIn("₹7", self.client.get(f"/?year=2031&month=1&account_id={other}").text)
        self.assertEqual(get_monthly_summary(2031, 1, account_id=acct).total_expense, 7)

        page = self.client.get(f"/transactions?year=2031&account_id={acct}").text
        self.assertIn("ONLY HERE", page)
        self.assertNotIn("ONLY HERE", self.client.get(f"/transactions?year=2031&account_id={other}").text)
        self.assertEqual(self.client.get("/transactions?account_id=abc").status_code, 200)
        self.assertEqual(self.client.get("/?account_id=-1").status_code, 200)


if __name__ == "__main__":
    unittest.main()
