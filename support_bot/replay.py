"""Replay saved customer messages through the bot, for example to compare two versions.

    uv run python -m support_bot.replay --file questions.jsonl --project verify-before

Each line of the file is {"question": "...", "channel": "web" | "email" | "slack"}.
"""
from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from support_bot.channels import handle_message
from support_bot.tracing import flush, setup_tracing


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay saved messages through the bot.")
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--project", required=True, help="Phoenix project to send traces to")
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()

    setup_tracing(args.project)
    rows = [json.loads(line) for line in args.file.read_text(encoding="utf-8").splitlines() if line.strip()]
    failures = 0

    def send(row: dict) -> None:
        nonlocal failures
        try:
            handle_message(row["question"], row.get("channel", "web"))
        except Exception:  # the failed request is still traced
            failures += 1

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        list(pool.map(send, rows))
    flush()
    print(f"Replayed {len(rows)} messages into '{args.project}' ({failures} failed)")


if __name__ == "__main__":
    main()
