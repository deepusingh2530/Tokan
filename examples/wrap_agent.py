"""Pattern 2: wrap an agent loop with the SDK — no proxy process needed.

    python examples/wrap_agent.py

Uses an injected fake transport so it runs offline. Delete ``transport=``
and set OPENAI_API_KEY to hit a real provider.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running from a source checkout without `pip install -e .`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tokan import OptimizedAgentClient

# A payload shaped like what a coding agent actually sends: heavy system
# prompt, a big code dump, and a real task.
MESSAGES = [
    {"role": "system", "content": "You are an expert software engineer. Follow the style guide. " * 30},
    {
        "role": "user",
        "content": "Refactor this monolithic module and explain the trade-offs:\n"
        "```python\n" + "\n".join(f"def handler_{i}(x): return x + {i}" for i in range(60)) + "\n```",
    },
]


def fake_transport(payload, api_key, timeout):
    """Stand-in for the upstream provider (offline demo)."""
    return {
        "choices": [
            {"message": {"role": "assistant", "content": "Here's the refactor plan..."}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 250, "total_tokens": 1450},
    }


def main() -> None:
    client = OptimizedAgentClient(
        upstream_provider="openai",
        api_key="demo",
        jev_confidence_threshold=0.88,
        cdn_namespace="demo",
        transport=fake_transport,
        price_per_million_tokens=2.50,
    )

    # --- First turn: compacted, forwarded upstream -------------------------
    first = client.chat_completion(MESSAGES, model="gpt-4o")
    print(f"turn 1 route     : {first.route}")
    print(f"turn 1 metrics   : {first.metrics}")

    # --- Identical task (fresh session, e.g. re-run after a crash) ---------
    second = client.chat_completion(MESSAGES, model="gpt-4o")
    print(f"turn 2 route     : {second.route}")
    print(f"turn 2 served    : {second.content!r}")
    print()
    print(client.session_report())

    # --- Compaction demo: how much text CDM removes before hashing ---------
    record = client.pipeline.normalizer.fingerprint(MESSAGES, {"model": "gpt-4o"})
    print(
        f"\nCDM compaction   : {record.original_tokens} -> {record.canonical_tokens} tokens "
        f"({record.saved_pct}% lighter before it ever reaches the model)"
    )


if __name__ == "__main__":
    main()
