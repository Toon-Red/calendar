"""Tests for Dream Calendar watchdog module."""
import http.client
import json
from unittest.mock import MagicMock, patch

import pytest

import watchdog


# ── check_health ───────────────────────────────────────────────────────────

def test_check_health_returns_dict_on_success():
    """check_health returns parsed JSON when service responds 200."""
    fake_body = json.dumps({"status": "ok", "version": "0.2.0"}).encode()

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = fake_body

    mock_conn = MagicMock()
    mock_conn.getresponse.return_value = mock_resp

    with patch.object(watchdog, "_WatchdogHTTPConnection", return_value=mock_conn):
        result = watchdog.check_health("127.0.0.1", 5041)

    assert result == {"status": "ok", "version": "0.2.0"}
    mock_conn.request.assert_called_once_with("GET", "/api/health")


def test_check_health_returns_none_on_connection_refused():
    """check_health returns None when the service is unreachable."""
    with patch.object(watchdog, "_WatchdogHTTPConnection",
                      side_effect=OSError("[WinError 10061] Connection refused")):
        result = watchdog.check_health("127.0.0.1", 5041)

    assert result is None


def test_check_health_returns_none_on_non_200():
    """check_health returns None when service returns non-200."""
    mock_resp = MagicMock()
    mock_resp.status = 503

    mock_conn = MagicMock()
    mock_conn.getresponse.return_value = mock_resp

    with patch.object(watchdog, "_WatchdogHTTPConnection", return_value=mock_conn):
        result = watchdog.check_health("127.0.0.1", 5041)

    assert result is None


def test_check_health_returns_none_on_timeout():
    """check_health returns None on socket timeout."""
    mock_conn = MagicMock()
    mock_conn.request.side_effect = OSError("timed out")

    with patch.object(watchdog, "_WatchdogHTTPConnection", return_value=mock_conn):
        result = watchdog.check_health("127.0.0.1", 5041)

    assert result is None


# ── wait_for_healthy ───────────────────────────────────────────────────────

def test_wait_for_healthy_succeeds_on_first_try():
    """wait_for_healthy returns True immediately if service is healthy."""
    with patch.object(watchdog, "check_health",
                      return_value={"status": "ok"}):
        assert watchdog.wait_for_healthy(retries=3, interval=0.01) is True


def test_wait_for_healthy_retries_then_succeeds():
    """wait_for_healthy retries and succeeds when service comes up."""
    responses = [None, None, {"status": "ok"}]
    with patch.object(watchdog, "check_health", side_effect=responses):
        assert watchdog.wait_for_healthy(retries=5, interval=0.01) is True


def test_wait_for_healthy_exhausts_retries():
    """wait_for_healthy returns False when retries are exhausted."""
    with patch.object(watchdog, "check_health", return_value=None):
        assert watchdog.wait_for_healthy(retries=3, interval=0.01) is False


# ── ensure_running ─────────────────────────────────────────────────────────

def test_ensure_running_already_healthy():
    """ensure_running returns True immediately if already healthy."""
    with patch.object(watchdog, "check_health",
                      return_value={"status": "ok", "version": "0.2.0"}):
        assert watchdog.ensure_running() is True


def test_ensure_running_check_only_returns_false():
    """In check-only mode, ensure_running returns False without starting."""
    with patch.object(watchdog, "check_health", return_value=None):
        with patch.object(watchdog, "start_service") as mock_start:
            result = watchdog.ensure_running(check_only=True)
            assert result is False
            mock_start.assert_not_called()


def test_ensure_running_starts_and_waits():
    """ensure_running starts the service and waits for health."""
    mock_proc = MagicMock()
    mock_proc.pid = 12345

    with patch.object(watchdog, "check_health", return_value=None):
        with patch.object(watchdog, "start_service", return_value=mock_proc):
            with patch.object(watchdog, "wait_for_healthy", return_value=True):
                assert watchdog.ensure_running() is True


def test_ensure_running_start_fails():
    """ensure_running returns False when start_service fails."""
    with patch.object(watchdog, "check_health", return_value=None):
        with patch.object(watchdog, "start_service", return_value=None):
            assert watchdog.ensure_running() is False


def test_ensure_running_starts_but_never_healthy():
    """ensure_running returns False when service starts but never becomes healthy."""
    mock_proc = MagicMock()
    mock_proc.pid = 99999

    with patch.object(watchdog, "check_health", return_value=None):
        with patch.object(watchdog, "start_service", return_value=mock_proc):
            with patch.object(watchdog, "wait_for_healthy", return_value=False):
                assert watchdog.ensure_running() is False
