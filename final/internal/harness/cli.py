"""Headless CLI for Harness Runtime 2.0.

Inspired by deepseek-harness's 'dsh' command-line interface,
this tool provides terminal-driven run, inspect, replay, and session management.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from internal.harness.runtime import HarnessRuntime


def main():
    parser = argparse.ArgumentParser(description="Harness Runtime 2.0 CLI (inspired by deepseek-harness dsh)")
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # Command: run
    run_p = subparsers.add_parser("run", help="Run an Agent task session")
    run_p.add_argument("--query", "-q", required=True, help="User query or task instruction")
    run_p.add_argument("--session-id", "-s", help="Optional session ID")
    run_p.add_argument("--storage", help="Path to events storage directory", default=".harness_events")

    # Command: inspect
    inspect_p = subparsers.add_parser("inspect", help="Inspect historical session events")
    inspect_p.add_argument("--session-id", "-s", required=True, help="Session ID to inspect")
    inspect_p.add_argument("--storage", help="Path to events storage directory", default=".harness_events")

    # Command: replay
    replay_p = subparsers.add_parser("replay", help="Replay session trajectory")
    replay_p.add_argument("--session-id", "-s", required=True, help="Session ID to replay")
    replay_p.add_argument("--storage", help="Path to events storage directory", default=".harness_events")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    runtime = HarnessRuntime.create_lightweight(storage_dir=args.storage)

    if args.command == "run":
        print(f"[Harness-INFO] Starting session for query: {args.query}")
        result = runtime.run(query=args.query, session_id=args.session_id)
        print(f"[Harness-OK] Status: {result.status}, Session: {result.session_id}")
        print(f"[Harness-OUTPUT] Output: {result.output}")
        print(f"[Harness-METRICS] Recorded events count: {len(result.events)}")

    elif args.command == "inspect":
        events = runtime.event_stream.get_events(args.session_id)
        print(f"[Harness-INSPECT] Inspecting session {args.session_id} ({len(events)} events):")
        for idx, ev in enumerate(events, 1):
            print(f"  [{idx}] Type: {ev.type.value:<15} Step: {ev.step_index:<3} Payload: {json.dumps(ev.payload, ensure_ascii=False)[:100]}")

    elif args.command == "replay":
        events = runtime.replay(args.session_id)
        print(f"[Harness-REPLAY] Replaying session {args.session_id}:")
        for ev in events:
            print(f"  -> ({ev.type.value}) {json.dumps(ev.payload, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
