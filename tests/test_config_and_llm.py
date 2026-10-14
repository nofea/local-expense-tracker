"""Tests for config.toml (Appendix A item 20), the extraction prompt and
client (items 22, 23), and the --dev launcher flag (item 24)."""

import os
import tempfile

# Never touch data/expenses.db: point the app at a throwaway DB before any
# core/web import reads the path (app_spec.md §11).
os.environ.setdefault(
    "EXPENSE_TRACKER_DB",
    os.path.join(tempfile.mkdtemp(prefix="expense-tests-"), "expenses.db"),
)

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
from fastapi.testclient import TestClient

import core.ollama_client as oc
from core.config import (
    PROJECT_ROOT,
    Config,
    ConfigError,
    LLMConfig,
    MoneyConfig,
    is_loopback,
    load_config,
)
from core.models import format_money
from web.app import app


class TestConfig(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "config.toml"

    def tearDown(self):
        self._tmp.cleanup()

    def load(self, text):
        self.path.write_text(text, encoding="utf-8")
        return load_config(self.path)

    def test_missing_file_gives_spec_defaults(self):
        c = load_config(self.path)
        self.assertEqual(
            (c.llm.host, c.llm.model, c.llm.chunk_timeout_s, c.llm.max_retries),
            ("http://localhost:11434", "llama3.1", 120.0, 1),
        )
        self.assertEqual((c.money.currency_code, c.money.minor_units), ("INR", 2))
        self.assertEqual((c.web.host, c.web.port), ("127.0.0.1", 8000))
        self.assertIsNone(c.source)

    def test_example_file_matches_defaults(self):
        example = load_config(PROJECT_ROOT / "config.example.toml")
        self.assertEqual(
            (example.llm, example.money, example.storage, example.web),
            (Config().llm, Config().money, Config().storage, Config().web),
        )

    def test_partial_file_keeps_other_defaults(self):
        c = self.load('[llm]\nmodel = "qwen2.5-coder:14b"\nchunk_timeout_s = 30\n')
        self.assertEqual((c.llm.model, c.llm.chunk_timeout_s), ("qwen2.5-coder:14b", 30.0))
        self.assertIsInstance(c.llm.chunk_timeout_s, float)
        self.assertEqual(c.llm.host, "http://localhost:11434")
        self.assertEqual(c.source, self.path)

    def test_fractional_timeout_is_accepted(self):
        self.assertEqual(self.load("[llm]\nchunk_timeout_s = 90.5\n").llm.chunk_timeout_s, 90.5)

    def test_errors_name_the_bad_key(self):
        cases = {
            "[llm]\nmodle = 'x'\n": "modle",
            "[database]\npath = 'x'\n": "database",
            "[web]\nport = '8000'\n": "port must be int",
            "[web]\nport = true\n": "port must be int",
            "[web]\nport = 70000\n": "1-65535",
            "[llm]\nhost = 'localhost:11434'\n": "http(s) URL",
            "[llm]\nmax_retries = -1\n": "max_retries",
            "[money]\nminor_units = 9\n": "minor_units",
            "[llm\n": "config.toml",
        }
        for text, needle in cases.items():
            with self.subTest(text=text), self.assertRaises(ConfigError) as ctx:
                self.load(text)
            self.assertIn(needle, str(ctx.exception))

    def test_db_path_resolution(self):
        c = self.load('[storage]\ndb_path = "elsewhere/x.db"\n')
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EXPENSE_TRACKER_DB", None)
            self.assertEqual(c.db_path, PROJECT_ROOT / "elsewhere" / "x.db")
            os.environ["EXPENSE_TRACKER_DB"] = "/tmp/override.db"
            self.assertEqual(c.db_path, Path("/tmp/override.db"))

    def test_loopback_detection(self):
        for host in ("localhost", "127.0.0.1", "127.1.2.3", "::1", "[::1]", "LOCALHOST"):
            self.assertTrue(is_loopback(host), host)
        for host in ("0.0.0.0", "192.168.1.5", "example.com", ""):
            self.assertFalse(is_loopback(host), host)
        self.assertFalse(Config(llm=LLMConfig(host="http://10.0.0.5:11434")).llm_is_local)
        self.assertTrue(Config().llm_is_local)


class TestFormatMoney(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(format_money(-123450, minor=True), "−₹1,234.50")
        self.assertEqual(format_money(123450, minor=True, signed=True), "+₹1,234.50")
        self.assertEqual(format_money(0, minor=True, signed=True), "₹0.00")
        self.assertEqual(format_money(90000.4, decimals=0), "₹90,000")
        self.assertEqual(format_money(None), "—")

    def test_symbol_from_config(self):
        cfg = Config(money=MoneyConfig(currency_code="EUR", currency_symbol="€"))
        with patch("core.models.get_config", return_value=cfg):
            self.assertEqual(format_money(-5, minor=True), "−€0.05")


def _chat_reply(payload):
    return {"message": {"content": payload if isinstance(payload, str) else json.dumps(payload)}}


GOOD = {"transactions": [{"transaction_date": "2026-03-02", "raw_description": "UPI-SWIGGY-8812", "amount": -450.5}]}
BAD = {"transactions": [{"transaction_date": "2026-03-02", "raw_description": "SECRET-NARRATION-123"}]}


@patch("core.ollama_client.is_ollama_available", return_value=True)
@patch("core.ollama_client._ollama")
class TestExtractionClient(unittest.TestCase):
    def config(self, **llm):
        cfg = Config(llm=LLMConfig(host="http://127.0.0.1:9999", chunk_timeout_s=7.0, **llm))
        return patch("core.ollama_client.get_config", return_value=cfg)

    def client(self, mock_ollama):
        return mock_ollama.Client.return_value

    def test_uses_configured_host_timeout_and_schema(self, mock_ollama, _avail):
        self.client(mock_ollama).chat.return_value = _chat_reply(GOOD)
        with self.config():
            txns = oc.extract_transactions("text", model="m")
        self.assertEqual(txns[0].amount, -450.5)
        mock_ollama.Client.assert_called_with(host="http://127.0.0.1:9999", timeout=7.0)
        kwargs = self.client(mock_ollama).chat.call_args.kwargs
        self.assertEqual(kwargs["options"], {"temperature": 0})
        self.assertEqual(kwargs["format"], oc.TransactionExtractionResult.model_json_schema())
        self.assertEqual(kwargs["messages"][0]["content"], oc.SYSTEM_PROMPT)

    def test_default_model_comes_from_config(self, mock_ollama, _avail):
        self.client(mock_ollama).chat.return_value = _chat_reply(GOOD)
        with self.config(model="qwen2.5-coder:14b"):
            oc.extract_transactions("text")
        self.assertEqual(self.client(mock_ollama).chat.call_args.kwargs["model"], "qwen2.5-coder:14b")

    def test_invalid_output_is_retried_then_succeeds(self, mock_ollama, _avail):
        self.client(mock_ollama).chat.side_effect = [_chat_reply(BAD), _chat_reply(GOOD)]
        with self.config(max_retries=1):
            self.assertEqual(len(oc.extract_transactions("text")), 1)
        self.assertEqual(self.client(mock_ollama).chat.call_count, 2)

    def test_invalid_output_gives_up_without_leaking_content(self, mock_ollama, _avail):
        self.client(mock_ollama).chat.return_value = _chat_reply(BAD)
        with self.config(max_retries=2), self.assertLogs("core.ollama_client", "INFO") as logs:
            with self.assertRaises(oc.OutputInvalid) as ctx:
                oc.extract_transactions("text")
        self.assertEqual(self.client(mock_ollama).chat.call_count, 3)
        self.assertNotIn("SECRET", str(ctx.exception))
        self.assertFalse(any("SECRET" in line for line in logs.output))

    def test_non_json_output(self, mock_ollama, _avail):
        self.client(mock_ollama).chat.return_value = _chat_reply("Sure! Here are your transactions")
        with self.config(max_retries=0), self.assertRaises(oc.OutputInvalid):
            oc.extract_transactions("text")

    def test_lenient_bare_list_is_accepted(self, mock_ollama, _avail):
        self.client(mock_ollama).chat.return_value = _chat_reply(GOOD["transactions"])
        with self.config():
            self.assertEqual(len(oc.extract_transactions("text")), 1)

    def test_timeout_fails_fast_without_retry(self, mock_ollama, _avail):
        self.client(mock_ollama).chat.side_effect = httpx.ReadTimeout("timed out")
        with self.config(max_retries=3):
            with self.assertRaisesRegex(oc.ExtractionTimeout, "within 7s"):
                oc.extract_transactions("text")
        self.assertEqual(self.client(mock_ollama).chat.call_count, 1)

    def test_metadata_uses_same_client(self, mock_ollama, _avail):
        self.client(mock_ollama).chat.return_value = _chat_reply(
            {"period_start": "2026-03-01", "period_end": "2026-03-31", "opening_balance": 10000, "closing_balance": None}
        )
        with self.config():
            meta = oc.extract_statement_metadata("text")
        self.assertEqual((meta.period_end, meta.opening_balance), ("2026-03-31", 10000.0))
        mock_ollama.Client.assert_called_with(host="http://127.0.0.1:9999", timeout=7.0)


class TestPrompt(unittest.TestCase):
    """Item 22: the prompt explains conventions and never restates the schema."""

    def test_schema_not_restated(self):
        for prompt in (oc.SYSTEM_PROMPT, oc.METADATA_PROMPT):
            self.assertNotIn('"properties"', prompt)
            self.assertNotIn('"type"', prompt)

    def test_conventions_covered(self):
        p = oc.SYSTEM_PROMPT.lower()
        for needle in ("credit card", "refunds", "withdrawals", "opening or closing balance",
                       "running balance", "day-first", "yyyy-mm-dd"):
            self.assertIn(needle, p)

    def test_example_output_is_valid_and_skips_opening_balance(self):
        example = oc.SYSTEM_PROMPT.split("Example output:", 1)[1]
        parsed = oc.TransactionExtractionResult.model_validate_json(example)
        self.assertEqual([t.amount for t in parsed.transactions], [-450.5, 90000.0])
        self.assertNotIn("Opening", example)


class TestWebConfig(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    @patch("web.app.is_ollama_available", return_value=False)
    def test_remote_llm_banner(self, _avail):
        self.assertNotIn("leaving this machine", self.client.get("/").text)
        remote = Config(llm=LLMConfig(host="http://10.0.0.5:11434"))
        with patch("web.app.get_config", return_value=remote):
            page = self.client.get("/").text
        self.assertIn("Statement text is leaving this machine", page)
        self.assertIn("http://10.0.0.5:11434", page)

    @patch("web.app.list_available_models", return_value=[])
    @patch("web.app.is_ollama_available", return_value=False)
    def test_upload_page_uses_configured_model_and_host(self, *_):
        cfg = Config(llm=LLMConfig(host="http://localhost:5555", model="mistral"))
        with patch("web.app.get_config", return_value=cfg):
            page = self.client.get("/upload").text
        self.assertIn('<option value="mistral">mistral (default)</option>', page)
        self.assertIn("http://localhost:5555", page)
        self.assertIn('data-currency="INR"', page)

    @patch("web.app.is_ollama_available", return_value=True)
    def test_blank_model_falls_back_to_config(self, _avail):
        cfg = Config(llm=LLMConfig(model="mistral"))
        with patch("web.app.get_config", return_value=cfg), patch("web.app.jobs.submit") as submit:
            self.client.post("/upload", files={"file": ("m.pdf", os.urandom(8).hex().encode(), "application/pdf")})
        job = submit.call_args[0][0]
        job.file_path.unlink(missing_ok=True)
        self.assertEqual(job.model, "mistral")


class TestRunWeb(unittest.TestCase):
    """Item 24: reload only with --dev; host/port from config."""

    def run_main(self, argv, cfg=None):
        import run_web

        with patch.object(sys, "argv", ["run_web.py", *argv]), \
             patch("run_web.uvicorn.run") as run, patch("run_web.init_db"), \
             patch("run_web.get_config", return_value=cfg or Config()):
            run_web.main()
        return run.call_args.kwargs

    def test_no_reload_by_default(self):
        kwargs = self.run_main([])
        self.assertEqual((kwargs["reload"], kwargs["host"], kwargs["port"]), (False, "127.0.0.1", 8000))

    def test_dev_flag_enables_reload(self):
        self.assertTrue(self.run_main(["--dev"])["reload"])

    def test_warns_on_exposed_hosts(self):
        from core.config import WebConfig

        cfg = Config(web=WebConfig(host="0.0.0.0", port=9000), llm=LLMConfig(host="http://gpu-box:11434"))
        with self.assertLogs("run_web", "WARNING") as logs:
            kwargs = self.run_main([], cfg)
        self.assertEqual((kwargs["host"], kwargs["port"]), ("0.0.0.0", 9000))
        self.assertEqual(len(logs.output), 2)


if __name__ == "__main__":
    unittest.main()
