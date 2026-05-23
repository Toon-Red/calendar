"""Dream Calendar watchdog — ensures the service stays reachable.

Usage:
    python watchdog.py              # Check health, start if down
    python watchdog.py --check      # Check only, exit 0/1
    python watchdog.py --retries 5  # Custom retry count after start

This script is designed to be called by:
- Windows Task Scheduler (on login / periodic)
- The EOD review playtest
- Any monitoring/orchestration layer

Exit codes:
    0 — service is healthy (already running or successfully started)
    1 — service is unreachable and could not be started
"""
import argparse
import http.client
import json
import logging
import os
import socket as _socket
import struct as _struct
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [watchdog] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("watchdog")

SERVICE_HOST = "127.0.0.1"
SERVICE_PORT = 5041
HEALTH_PATH = "/api/health"
APP_SCRIPT = Path(__file__).parent / "app.py"


# ── SO_REUSEADDR + SO_LINGER connection (mirrors app.py pattern) ───────────

class _WatchdogHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection with SO_REUSEADDR + SO_LINGER for clean loopback."""

    def connect(self):
        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_LINGER,
                        _struct.pack('ii', 1, 0))
        sock.settimeout(self.timeout)
        try:
            sock.connect((self.host, self.port))
        except OSError:
            sock.close()
            raise
        self.sock = sock


# ── Core functions ─────────────────────────────────────────────────────────

READY_PATH = "/api/ready"


def check_health(host: str = SERVICE_HOST, port: int = SERVICE_PORT,
                 timeout: float = 5.0) -> dict | None:
    """Probe /api/health. Returns parsed JSON on success, None on failure."""
    try:
        conn = _WatchdogHTTPConnection(host, port, timeout=timeout)
        conn.request("GET", HEALTH_PATH)
        resp = conn.getresponse()
        if resp.status == 200:
            body = json.loads(resp.read())
            conn.close()
            return body
        conn.close()
    except (OSError, http.client.HTTPException, json.JSONDecodeError):
        pass
    return None


def check_ready(host: str = SERVICE_HOST, port: int = SERVICE_PORT,
                timeout: float = 5.0) -> dict | None:
    """Probe /api/ready (deep readiness). Returns parsed JSON on success, None on failure."""
    try:
        conn = _WatchdogHTTPConnection(host, port, timeout=timeout)
        conn.request("GET", READY_PATH)
        resp = conn.getresponse()
        body = json.loads(resp.read())
        conn.close()
        # /api/ready returns 200 or 503; both carry useful JSON
        return body
    except (OSError, http.client.HTTPException, json.JSONDecodeError):
        pass
    return None


def start_service(host: str = SERVICE_HOST, port: int = SERVICE_PORT) -> subprocess.Popen | None:
    """Start Dream Calendar as a detached background process.

    Uses CREATE_NEW_PROCESS_GROUP + DETACHED_PROCESS on Windows so the
    child survives the parent exiting.  On non-Windows, uses preexec_fn
    to start a new session.
    """
    cmd = [sys.executable, str(APP_SCRIPT), "--host", host, "--port", str(port)]
    log.info("Starting Dream Calendar: %s", " ".join(cmd))

    try:
        kwargs: dict = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "stdin": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            # DETACHED_PROCESS (0x08) + CREATE_NEW_PROCESS_GROUP (0x200)
            kwargs["creationflags"] = 0x00000008 | 0x00000200
        else:
            kwargs["start_new_session"] = True

        from windowless_subprocess import Popen as _wl_popen  # d48d4ddb
        proc = _wl_popen(cmd, **kwargs)
        log.info("Started PID %d", proc.pid)
        return proc
    except OSError as exc:
        log.error("Failed to start service: %s", exc)
        return None


def wait_for_healthy(host: str = SERVICE_HOST, port: int = SERVICE_PORT,
                     retries: int = 10, interval: float = 1.0) -> bool:
    """Poll health endpoint until it responds or retries exhausted."""
    for attempt in range(1, retries + 1):
        result = check_health(host, port, timeout=3.0)
        if result and result.get("status") == "ok":
            log.info("Healthy after %d attempt(s): %s", attempt, result)
            return True
        log.debug("Attempt %d/%d — not yet healthy", attempt, retries)
        time.sleep(interval)
    return False


def ensure_running(host: str = SERVICE_HOST, port: int = SERVICE_PORT,
                   check_only: bool = False, retries: int = 10) -> bool:
    """Main watchdog logic: check health, optionally start the service.

    Returns True if the service is healthy at exit, False otherwise.
    """
    result = check_health(host, port)
    if result and result.get("status") == "ok":
        log.info("Dream Calendar is healthy: %s", result)
        return True

    log.warning("Dream Calendar is NOT responding at %s:%d", host, port)

    if check_only:
        return False

    # Attempt to start the service
    proc = start_service(host, port)
    if proc is None:
        return False

    # Wait for it to become healthy
    if wait_for_healthy(host, port, retries=retries):
        return True

    log.error("Service started (PID %d) but did not become healthy within %d retries",
              proc.pid, retries)
    return False


def status_report(host: str = SERVICE_HOST, port: int = SERVICE_PORT,
                  check_only: bool = False, retries: int = 10) -> dict:
    """Run ensure_running and produce a structured status report.

    Returns a dict suitable for JSON serialization, used by the EOD UI
    playtest and other automation to get a machine-readable service status.
    """
    report: dict = {
        "service": "dream-calendar",
        "host": host,
        "port": port,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # First, check liveness
    health = check_health(host, port)
    was_running = health is not None and health.get("status") == "ok"
    report["was_running"] = was_running

    if not was_running and not check_only:
        # Attempt auto-start
        proc = start_service(host, port)
        if proc is None:
            report["action"] = "start_failed"
            report["healthy"] = False
            report["ready"] = False
            return report
        report["action"] = "started"
        report["pid"] = proc.pid
        if not wait_for_healthy(host, port, retries=retries):
            report["healthy"] = False
            report["ready"] = False
            return report
        health = check_health(host, port)
    elif not was_running:
        report["action"] = "check_only"
        report["healthy"] = False
        report["ready"] = False
        return report
    else:
        report["action"] = "none_needed"

    report["healthy"] = True
    report["health"] = health

    # Deep readiness check
    ready = check_ready(host, port)
    if ready and ready.get("ready"):
        report["ready"] = True
        report["readiness"] = ready
    else:
        report["ready"] = False
        report["readiness"] = ready

    return report


# ── CLI entrypoint ─────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Dream Calendar watchdog — check health and auto-start if needed."
    )
    ap.add_argument("--host", default=SERVICE_HOST,
                    help="Service host (default: %(default)s)")
    ap.add_argument("--port", type=int, default=SERVICE_PORT,
                    help="Service port (default: %(default)s)")
    ap.add_argument("--check", action="store_true",
                    help="Check only — don't attempt to start the service")
    ap.add_argument("--retries", type=int, default=10,
                    help="Max health-check retries after starting (default: %(default)s)")
    ap.add_argument("--json", action="store_true", dest="json_output",
                    help="Output structured JSON report (for EOD playtest integration)")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="Enable debug logging")
    args = ap.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.json_output:
        # Structured output mode for EOD playtest and other automation
        report = status_report(
            host=args.host,
            port=args.port,
            check_only=args.check,
            retries=args.retries,
        )
        print(json.dumps(report, indent=2))
        sys.exit(0 if report.get("healthy") and report.get("ready") else 1)
    else:
        healthy = ensure_running(
            host=args.host,
            port=args.port,
            check_only=args.check,
            retries=args.retries,
        )
        sys.exit(0 if healthy else 1)


if __name__ == "__main__":
    main()
