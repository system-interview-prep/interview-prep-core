"""Long-running DNS/TLS/worker soak for the production voice transport."""

from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse


def now() -> str:
    return datetime.now(UTC).isoformat()


def probe(host: str, port: int, timeout: float) -> dict:
    started = time.perf_counter()
    addresses = sorted({item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
    dns_ms = round((time.perf_counter() - started) * 1000, 2)
    tls_started = time.perf_counter()
    with socket.create_connection((host, port), timeout=timeout) as connection:
        with ssl.create_default_context().wrap_socket(connection, server_hostname=host) as secured:
            tls_version = secured.version()
    return {
        "host": host,
        "addresses": addresses,
        "dnsMs": dns_ms,
        "tlsMs": round((time.perf_counter() - tls_started) * 1000, 2),
        "tlsVersion": tls_version,
    }


def worker_state(container: str) -> dict:
    command = [
        "docker",
        "inspect",
        "--format",
        "{{json .State}}",
        container,
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
    if completed.returncode != 0:
        return {"ok": False, "error": completed.stderr.strip()[:300]}
    state = json.loads(completed.stdout)
    return {
        "ok": state.get("Status") == "running",
        "status": state.get("Status"),
        "startedAt": state.get("StartedAt"),
        "exitCode": state.get("ExitCode"),
    }


def summarize(events_path: Path, started_at: str, requested_hours: float) -> dict:
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line]
    failures = [event for event in events if not event.get("ok")]
    return {
        "startedAt": started_at,
        "finishedAt": now(),
        "requestedHours": requested_hours,
        "samples": len(events),
        "successfulSamples": len(events) - len(failures),
        "failedSamples": len(failures),
        "successRate": round((len(events) - len(failures)) / len(events), 6) if events else 0.0,
        "failureExamples": failures[:20],
        "scope": "DNS, TLS, and Docker worker state only; not an audio/STT media-path soak",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--interval-seconds", type=float, default=30.0)
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--livekit-url", default=os.getenv("LIVEKIT_URL", ""))
    parser.add_argument("--gateway-host", default="agent-gateway.livekit.cloud")
    parser.add_argument("--container", default="interview-prep-core-voice-agent-1")
    args = parser.parse_args()
    livekit_host = urlparse(args.livekit_url).hostname
    if not livekit_host:
        raise SystemExit("--livekit-url or LIVEKIT_URL is required")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    started_at = now()
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    events_path = args.output_dir / f"transport-soak-{run_id}.jsonl"
    summary_path = args.output_dir / f"transport-soak-{run_id}.summary.json"
    deadline = time.monotonic() + args.hours * 3600
    try:
        while time.monotonic() < deadline:
            event: dict = {"timestamp": now(), "ok": True}
            try:
                event["endpoints"] = [
                    probe(livekit_host, 443, args.timeout_seconds),
                    probe(args.gateway_host, 443, args.timeout_seconds),
                ]
                event["worker"] = worker_state(args.container)
                event["ok"] = bool(event["worker"]["ok"])
            except Exception as exc:
                event.update(ok=False, error=f"{type(exc).__name__}: {exc}")
            with events_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            summary_path.write_text(
                json.dumps(
                    summarize(events_path, started_at, args.hours),
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            time.sleep(max(1.0, args.interval_seconds))
    except KeyboardInterrupt:
        pass
    finally:
        summary_path.write_text(
            json.dumps(summarize(events_path, started_at, args.hours), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(summary_path)


if __name__ == "__main__":
    main()
