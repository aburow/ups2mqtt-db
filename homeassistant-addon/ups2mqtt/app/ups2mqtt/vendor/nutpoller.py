#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Minimalist read-only NUT/upsd network poller."""

import argparse
import json
import socket
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class PollResult:
    """Result of a UPS poll."""

    host: str
    port: int
    ups_name: str
    variables: dict
    timestamp: datetime
    success: bool
    error: str = None

    def to_dict(self):
        return {
            "host": self.host,
            "port": self.port,
            "ups_name": self.ups_name,
            "timestamp": self.timestamp.isoformat(),
            "success": self.success,
            "error": self.error,
            "variables": self.variables,
        }


def parse_nut_response(lines, ups_name):
    """Parse NUT LIST VAR response lines."""
    variables = {}

    for line in lines:
        # Handle error responses
        if line.startswith("ERR "):
            raise ValueError(line)

        # Skip non-VAR lines
        if not line.startswith("VAR "):
            continue

        # Parse: VAR ups_name variable.name "value"
        parts = line.split(None, 3)  # Split into max 4 parts
        if len(parts) != 4:
            continue

        _, got_ups, var_name, quoted_value = parts

        # Verify UPS name matches
        if got_ups != ups_name:
            continue

        # Remove quotes from value
        if quoted_value.startswith('"') and quoted_value.endswith('"'):
            value = quoted_value[1:-1]
        else:
            value = quoted_value

        variables[var_name] = value

    return variables


def poll_ups(host, port, ups_name, timeout=5.0):
    """Poll UPS and return variables."""
    try:
        # Connect to NUT server
        sock = socket.create_connection((host, port), timeout=timeout)

        try:
            # Send LIST VAR command
            command = f"LIST VAR {ups_name}\n"
            sock.sendall(command.encode("utf-8"))

            # Read response
            lines = []
            buffer = ""

            while True:
                chunk = sock.recv(4096).decode("utf-8")
                if not chunk:
                    break

                buffer += chunk

                # Process complete lines
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.rstrip("\r")
                    lines.append(line)

                    # Check for terminator or error
                    if line.startswith(f"END LIST VAR {ups_name}") or line.startswith(
                        "ERR "
                    ):
                        break

                if lines and (
                    lines[-1].startswith(f"END LIST VAR {ups_name}")
                    or lines[-1].startswith("ERR ")
                ):
                    break

            # Parse variables
            variables = parse_nut_response(lines, ups_name)

            return PollResult(
                host=host,
                port=port,
                ups_name=ups_name,
                variables=variables,
                timestamp=datetime.now(timezone.utc),
                success=True,
            )

        finally:
            sock.close()

    except Exception as e:
        return PollResult(
            host=host,
            port=port,
            ups_name=ups_name,
            variables={},
            timestamp=datetime.now(timezone.utc),
            success=False,
            error=str(e),
        )


def format_table(result):
    """Format result as a table."""
    lines = [
        f"Target: {result.ups_name}@{result.host}:{result.port}",
        f"Timestamp: {result.timestamp.isoformat()}",
        f"Success: {result.success}",
    ]

    if result.error:
        lines.append(f"Error: {result.error}")

    if result.variables:
        lines.append(f"\nVariables ({len(result.variables)}):")
        for var_name in sorted(result.variables.keys()):
            lines.append(f"  {var_name}: {result.variables[var_name]}")
    else:
        lines.append("\nNo variables received")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Minimalist read-only NUT/upsd network poller"
    )
    parser.add_argument(
        "--host", default="192.168.101.43", help="NUT server hostname or IP"
    )
    parser.add_argument("--port", type=int, default=3493, help="NUT server port")
    parser.add_argument("--ups", default="devups", help="UPS name")
    parser.add_argument(
        "--timeout", type=float, default=5.0, help="Socket timeout in seconds"
    )
    parser.add_argument(
        "--interval", type=float, default=15.0, help="Poll interval for recurring mode"
    )
    parser.add_argument("--once", action="store_true", help="Poll once and exit")
    parser.add_argument(
        "--json", action="store_true", help="Output JSON format (only with --once)"
    )

    args = parser.parse_args()

    if args.once:
        # Single poll
        result = poll_ups(args.host, args.port, args.ups, args.timeout)

        if args.json:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            print(format_table(result))

        sys.exit(0 if result.success else 1)
    else:
        # Recurring poll
        print(
            f"Polling {args.ups}@{args.host}:{args.port} every {args.interval}s (Ctrl+C to stop)"
        )
        poll_count = 0

        try:
            while True:
                poll_count += 1
                print(f"\n[Poll #{poll_count}]")

                result = poll_ups(args.host, args.port, args.ups, args.timeout)

                if result.success:
                    print(f"Success: {len(result.variables)} variables")
                else:
                    print(f"Failed: {result.error}")

                time.sleep(args.interval)

        except KeyboardInterrupt:
            print(f"\nStopped after {poll_count} polls")
            sys.exit(0)


if __name__ == "__main__":
    main()
