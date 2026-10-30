"""Send realistic traffic to the support bot so its traces show up in Phoenix.

    uv run python -m scripts.seed_traffic --n 150

Messages come from scripts/questions.yaml: mostly normal questions, plus out-of-scope
questions, prompt-injection attempts and messages that contain card numbers, spread over
the web, email and Slack channels the way real support traffic would be.
"""
from __future__ import annotations

import argparse
import random
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from support_bot.channels import handle_message
from support_bot.tracing import flush, setup_tracing

QUESTIONS = Path(__file__).with_name("questions.yaml")
CATEGORY_WEIGHTS = {
    "in_scope": 0.48, "paraphrase": 0.14, "out_of_scope": 0.20,
    "injection": 0.06, "pii": 0.06, "multi_part": 0.06,
}
CHANNEL_WEIGHTS = {"web": 0.55, "email": 0.30, "slack": 0.15}
NAMES = ["Priya", "Daniel", "Mei", "Arjun", "Sofia", "Tom", "Aisha", "Lucas"]


def as_email(question: str, rng: random.Random) -> str:
    name = rng.choice(NAMES)
    body = f"Hi Orbitask team,\n\n{question}\n\nThanks,\n{name}\n-- \n{name} | Operations"
    if rng.random() < 0.3:
        body += "\n\nOn Mon, 2 Mar 2026, Orbitask Support wrote:\n> Thanks for reaching out!"
    return body


def build_traffic(n: int, seed: int) -> list[tuple[str, str, str]]:
    """Return n (category, channel, message) tuples, reproducible for a given seed."""
    bank = yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))
    rng = random.Random(seed)
    traffic = []
    for _ in range(n):
        category = rng.choices(list(CATEGORY_WEIGHTS), weights=CATEGORY_WEIGHTS.values())[0]
        channel = rng.choices(list(CHANNEL_WEIGHTS), weights=CHANNEL_WEIGHTS.values())[0]
        question = rng.choice(bank[category])
        message = as_email(question, rng) if channel == "email" else question
        traffic.append((category, channel, message))
    return traffic


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--n", type=int, default=150, help="number of messages to send")
    parser.add_argument("--seed", type=int, default=7, help="random seed, for repeatable runs")
    parser.add_argument("--concurrency", type=int, default=6)
    args = parser.parse_args()

    setup_tracing()
    traffic = build_traffic(args.n, args.seed)
    errors: Counter = Counter()
    started = time.time()

    def send(item: tuple[str, str, str]) -> None:
        category, channel, message = item
        try:
            handle_message(message, channel)
        except Exception as exc:  # keep going; the failed span is still in Phoenix
            errors[type(exc).__name__] += 1

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        for i, _ in enumerate(pool.map(send, traffic), start=1):
            if i % 25 == 0:
                print(f"  sent {i}/{len(traffic)}")
    flush()

    print(f"Sent {len(traffic)} messages in {time.time() - started:.0f}s")
    print("By category:", dict(Counter(c for c, _, _ in traffic)))
    print("By channel: ", dict(Counter(ch for _, ch, _ in traffic)))
    if errors:
        print("Errors:", dict(errors))


if __name__ == "__main__":
    main()
