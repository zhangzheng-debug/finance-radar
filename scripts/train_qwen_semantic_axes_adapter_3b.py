#!/usr/bin/env python3
"""Run the hardened semantic-axes trainer with the Qwen2.5-3B profile.

All dataset, prompt, provenance, membership, tokenizer, adapter and hardware
checks remain in ``train_qwen_semantic_axes_adapter``.  This entry point only
changes the pinned base-model identity and architecture profile.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import train_qwen_semantic_axes_adapter as driver


MODEL_ID = "Qwen/Qwen2.5-3B-Instruct"
MODEL_PROFILE = {
    "model_type": "qwen2",
    "architecture": "Qwen2ForCausalLM",
    "hidden_size": 2048,
    "intermediate_size": 11008,
    "num_hidden_layers": 36,
    "num_attention_heads": 16,
    "num_key_value_heads": 2,
    "vocab_size": 151936,
}


def configure_driver() -> None:
    driver.EXPECTED_BASE_MODEL_ID = MODEL_ID
    driver.EXPECTED_QWEN_PROFILE = dict(MODEL_PROFILE)


def main(argv: list[str] | None = None) -> int:
    configure_driver()
    return driver.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
