from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    ledger_db: Path = ROOT / "data" / "finance_radar.sqlite3"
    operations_db: Path = ROOT / "data" / "finance_radar_operations.sqlite3"
    artifact_dir: Path = ROOT / "artifacts"
    evidence_object_dir: Path = ROOT / "data" / "evidence_objects"
    replay_dir: Path = ROOT / "replay" / "cases"
    demo_mode: str = "RECENT_CAPTURE"
    admin_token: str | None = None
    api_base_url: str = "http://127.0.0.1:8000"
    web_base_url: str = "http://127.0.0.1:8501"
    api_rate_limit_per_minute: int = 180
    evidence_llm_url: str | None = None
    evidence_llm_model: str = "qwen2.5-0.5b-instruct-q4_k_m"
    evidence_llm_timeout_seconds: float = 30.0
    evidence_llm_max_tokens: int = 900

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            ledger_db=Path(os.getenv("FINANCE_RADAR_DB", cls.ledger_db)).resolve(),
            operations_db=Path(os.getenv("FINANCE_RADAR_OPS_DB", cls.operations_db)).resolve(),
            artifact_dir=Path(os.getenv("FINANCE_RADAR_ARTIFACT_DIR", cls.artifact_dir)).resolve(),
            evidence_object_dir=Path(
                os.getenv("FINANCE_RADAR_EVIDENCE_OBJECT_DIR", cls.evidence_object_dir)
            ).resolve(),
            replay_dir=Path(os.getenv("FINANCE_RADAR_REPLAY_DIR", cls.replay_dir)).resolve(),
            demo_mode=os.getenv("FINANCE_RADAR_DEMO_MODE", "RECENT_CAPTURE").upper(),
            admin_token=os.getenv("FINANCE_RADAR_ADMIN_TOKEN") or None,
            api_base_url=os.getenv("FINANCE_RADAR_API_URL", "http://127.0.0.1:8000").rstrip("/"),
            web_base_url=os.getenv("FINANCE_RADAR_WEB_URL", "http://127.0.0.1:8501").rstrip("/"),
            api_rate_limit_per_minute=max(
                0,
                int(os.getenv("FINANCE_RADAR_API_RATE_LIMIT_PER_MINUTE", "180")),
            ),
            evidence_llm_url=os.getenv("FINANCE_RADAR_EVIDENCE_LLM_URL") or None,
            evidence_llm_model=os.getenv(
                "FINANCE_RADAR_EVIDENCE_LLM_MODEL",
                "qwen2.5-0.5b-instruct-q4_k_m",
            ),
            evidence_llm_timeout_seconds=max(
                1.0,
                float(os.getenv("FINANCE_RADAR_EVIDENCE_LLM_TIMEOUT_SECONDS", "30")),
            ),
            evidence_llm_max_tokens=max(
                128,
                int(os.getenv("FINANCE_RADAR_EVIDENCE_LLM_MAX_TOKENS", "900")),
            ),
        )

    @property
    def model_artifact(self) -> Path:
        return self.artifact_dir / "risk_router.joblib"

    @property
    def model_card(self) -> Path:
        return self.artifact_dir / "risk_router_model_card.json"
