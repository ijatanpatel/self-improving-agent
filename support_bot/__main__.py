"""Ask the bot a question from the terminal:

    uv run python -m support_bot "How do I reset my password?" --channel web
"""
import argparse

from support_bot.channels import CHANNELS, handle_message
from support_bot.tracing import flush, setup_tracing


def main() -> None:
    parser = argparse.ArgumentParser(description="Ask Orbi, the Orbitask support bot.")
    parser.add_argument("question")
    parser.add_argument("--channel", choices=CHANNELS, default="web")
    args = parser.parse_args()

    setup_tracing()
    reply = handle_message(args.question, args.channel)
    flush()

    print(reply.text)
    print(f"\n[confidence={reply.confidence} citations={reply.citations} trace={reply.trace_id}]")


if __name__ == "__main__":
    main()
