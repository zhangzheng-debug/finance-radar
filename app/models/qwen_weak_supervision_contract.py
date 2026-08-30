"""Versioned, build-only contract for weakly supervised Qwen datasets.

This module is deliberately separate from :mod:`qwen_risk_contract`: importing
it from a dataset builder must not change the production runtime prompt or its
default output contract.
"""

from __future__ import annotations

import hashlib


QWEN_WEAK_SUPERVISION_VERSION = "qwen-core-weak-supervision-v11"
QWEN_WEAK_PROMPT_VERSION = "qwen-core-axes-prompt-v11"
QWEN_WEAK_MODEL_OUTPUT_CONTRACT = "core-axes-v1"
QWEN_WEAK_PAIR_MULTIPLIER_CONTRACT = "qwen-core-pair-multipliers-v11"

# Keep the established task semantics intact.  The only output-contract change
# is that the assistant is trained on the two independent axes; the two derived
# fields remain in metadata as the four-field core-v1 semantic truth.
QWEN_WEAK_SYSTEM_PROMPT = (
    "你是金融雷达的语义风险分类器。只根据所给文本判断对焦点资产的极性与做空风险重大性；"
    "不判断证据真假，不补充外部事实，不使用事后价格，不给投资建议。"
    "区分已发生事实、正式决定、提议、风险因素、合同定义与历史重述。"
    "破产重组、确定退市、已发生违约、重大监管处罚、关键临床失败可构成重大负面；"
    "普通风险披露、假设性条款、已解决事项、常规治理和有偿并购退出不得仅凭关键词判为重大负面。"
    "融资同时考虑获得资金与稀释，明确改善或成功结果可判正面。"
    "仅输出包含 materiality 与 polarity 两个键的指定 JSON。"
)
QWEN_WEAK_PROMPT_SHA256 = hashlib.sha256(
    QWEN_WEAK_SYSTEM_PROMPT.encode("utf-8")
).hexdigest()

# Tuple form makes the checked-in default immutable while remaining trivial to
# serialize and copy into an explicit per-build configuration.
QWEN_WEAK_DEFAULT_PAIR_MULTIPLIERS = (
    ("MATERIAL_ADVERSE", "ADVERSE", 2),
    ("NOT_MATERIAL_ADVERSE", "ADVERSE", 4),
    ("NOT_MATERIAL_ADVERSE", "MIXED", 3),
    ("NOT_MATERIAL_ADVERSE", "POSITIVE", 2),
)
