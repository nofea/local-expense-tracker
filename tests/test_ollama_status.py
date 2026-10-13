"""Unit tests for Ollama status detection and web UI status rendering."""

import os
import tempfile

# Never touch data/expenses.db: point the app at a throwaway DB before any
# core/web import reads the path (app_spec.md §11).
os.environ.setdefault(
    "EXPENSE_TRACKER_DB",
    os.path.join(tempfile.mkdtemp(prefix="expense-tests-"), "expenses.db"),
)

import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from core.ollama_client import is_ollama_available, list_available_models, extract_transactions
from web.app import app


class TestOllamaStatusDetection(unittest.TestCase):
    """Tests for core.ollama_client functions."""

    def setUp(self):
        # Reset cache before each test
        import core.ollama_client as oc
        oc._ollama_status_cache = (0.0, False)

    @patch("core.ollama_client._ollama")
    def test_is_ollama_available_when_reachable(self, mock_ollama):
        mock_client = MagicMock()
        mock_client.list.return_value = {"models": [{"model": "llama3.1"}]}
        mock_ollama.Client.return_value = mock_client

        self.assertTrue(is_ollama_available(force=True))

    @patch("core.ollama_client._ollama")
    def test_is_ollama_available_when_connection_fails(self, mock_ollama):
        mock_client = MagicMock()
        mock_client.list.side_effect = ConnectionError("Failed to connect")
        mock_ollama.Client.return_value = mock_client

        self.assertFalse(is_ollama_available(force=True))

    @patch("core.ollama_client._ollama", None)
    def test_is_ollama_available_when_package_missing(self):
        self.assertFalse(is_ollama_available(force=True))

    @patch("core.ollama_client._ollama")
    def test_list_available_models_returns_empty_when_offline(self, mock_ollama):
        mock_client = MagicMock()
        mock_client.list.side_effect = ConnectionError("Offline")
        mock_ollama.Client.return_value = mock_client

        models = list_available_models()
        self.assertEqual(models, [])

    @patch("core.ollama_client._ollama")
    def test_list_available_models_returns_models_when_online(self, mock_ollama):
        mock_client = MagicMock()
        mock_client.list.return_value = {
            "models": [
                {"name": "llama3.1:latest", "size": 4000000000, "modified_at": "2024-01-01"},
                {"name": "mistral:latest", "size": 3500000000, "modified_at": "2024-01-01"},
            ]
        }
        mock_ollama.Client.return_value = mock_client

        models = list_available_models()
        self.assertEqual(len(models), 2)
        self.assertEqual(models[0]["name"], "llama3.1:latest")

    @patch("core.ollama_client.is_ollama_available", return_value=False)
    def test_extract_transactions_raises_when_offline(self, mock_avail):
        with self.assertRaises(RuntimeError) as ctx:
            extract_transactions("Some statement text")
        self.assertIn("Ollama is not running", str(ctx.exception))


class TestWebOllamaStatusRendering(unittest.TestCase):
    """Tests for web UI rendering of the Ollama status badge."""

    def setUp(self):
        self.client = TestClient(app)

    @patch("web.app.is_ollama_available", return_value=False)
    def test_dashboard_shows_ollama_offline_when_not_installed(self, mock_avail):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("status-dot offline", res.text)
        self.assertIn("Ollama Offline", res.text)
        self.assertNotIn("status-dot online", res.text)
        self.assertNotIn("Ollama Connected", res.text)

    @patch("web.app.is_ollama_available", return_value=True)
    def test_dashboard_shows_ollama_connected_when_online(self, mock_avail):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("status-dot online", res.text)
        self.assertIn("Ollama Connected", res.text)
        self.assertNotIn("status-dot offline", res.text)
        self.assertNotIn("Ollama Offline", res.text)

    @patch("web.app.is_ollama_available", return_value=False)
    def test_upload_page_shows_ollama_offline_and_warning(self, mock_avail):
        res = self.client.get("/upload")
        self.assertEqual(res.status_code, 200)
        self.assertIn("status-dot offline", res.text)
        self.assertIn("Ollama Offline", res.text)
        self.assertIn("Ollama is not reachable at http://localhost:11434", res.text)

    @patch("web.app.is_ollama_available", return_value=False)
    def test_transactions_page_shows_ollama_offline(self, mock_avail):
        res = self.client.get("/transactions")
        self.assertEqual(res.status_code, 200)
        self.assertIn("status-dot offline", res.text)
        self.assertIn("Ollama Offline", res.text)

    @patch("web.app.is_ollama_available", return_value=False)
    def test_rules_page_shows_ollama_offline(self, mock_avail):
        res = self.client.get("/rules")
        self.assertEqual(res.status_code, 200)
        self.assertIn("status-dot offline", res.text)
        self.assertIn("Ollama Offline", res.text)

    @patch("web.app.is_ollama_available", return_value=False)
    def test_api_status_endpoint_offline(self, mock_avail):
        res = self.client.get("/api/ollama-status")
        self.assertEqual(res.status_code, 200)
        self.assertIn("status-dot offline", res.text)
        self.assertIn("Ollama Offline", res.text)

    @patch("web.app.is_ollama_available", return_value=True)
    def test_api_status_endpoint_online(self, mock_avail):
        res = self.client.get("/api/ollama-status")
        self.assertEqual(res.status_code, 200)
        self.assertIn("status-dot online", res.text)
        self.assertIn("Ollama Connected", res.text)

    @patch("web.app.is_ollama_available", return_value=False)
    def test_upload_file_fails_fast_when_offline(self, mock_avail):
        res = self.client.post(
            "/upload",
            files={"file": ("stmt.csv", b"Date,Description,Amount\n2024-01-01,Store,-10.00", "text/csv")},
            data={"model": "llama3.1"},
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn("Processing Failed", res.text)
        self.assertIn("Ollama is not running or unreachable", res.text)


if __name__ == "__main__":
    unittest.main()
