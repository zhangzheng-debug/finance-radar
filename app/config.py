from __future__ import annotations

import os
import json
import hashlib
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _reviewer_principals_payload() -> str:
    """Load reviewer principals from one unambiguous secret source.

    Compose/local development can use the JSON environment variable.  The
    production systemd unit uses ``LoadCredential=`` so the principal tokens do
    not enter the service environment or ``systemctl show`` output.
    """

    raw = os.getenv("FINANCE_RADAR_REVIEWER_PRINCIPALS_JSON", "").strip()
    credential_directory = os.getenv("CREDENTIALS_DIRECTORY", "").strip()
    credential_path = (
        Path(credential_directory) / "reviewer-principals.json"
        if credential_directory
        else None
    )
    credential_present = bool(credential_path and credential_path.is_file())
    if raw and credential_present:
        raise ValueError("reviewer principals must use either environment JSON or a systemd credential, not both")
    if credential_present:
        if credential_path.stat().st_size > 64 * 1024:
            raise ValueError("reviewer principals credential exceeds 64 KiB")
        raw = credential_path.read_text(encoding="utf-8").strip()
    return raw


def _reviewer_principals_from_env() -> tuple[tuple[str, str, str], ...]:
    """Parse credential-bound human reviewers without accepting shared aliases."""

    raw = _reviewer_principals_payload()
    if not raw:
        return ()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("FINANCE_RADAR_REVIEWER_PRINCIPALS_JSON must be valid JSON") from exc
    if not isinstance(payload, list):
        raise ValueError("FINANCE_RADAR_REVIEWER_PRINCIPALS_JSON must be a list")
    principals: list[tuple[str, str, str]] = []
    seen_ids: set[str] = set()
    seen_tokens: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("each reviewer principal must be an object")
        principal_id = str(item.get("principal_id") or "").strip()
        role = str(item.get("role") or "").strip().upper()
        token = str(item.get("token") or "").strip()
        if len(principal_id) < 3 or role not in {"REVIEWER", "ARBITER"} or len(token) < 24:
            raise ValueError("reviewer principal requires principal_id, role and a 24+ character token")
        normalized_id = principal_id.casefold()
        token_fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if normalized_id in seen_ids or token_fingerprint in seen_tokens:
            raise ValueError("reviewer principal IDs and tokens must be unique")
        seen_ids.add(normalized_id)
        seen_tokens.add(token_fingerprint)
        principals.append((principal_id, role, token))
    return tuple(principals)


@dataclass(frozen=True)
class Settings:
    ledger_db: Path = ROOT / "data" / "finance_radar.sqlite3"
    operations_db: Path = ROOT / "data" / "finance_radar_operations.sqlite3"
    artifact_dir: Path = ROOT / "artifacts"
    evidence_object_dir: Path = ROOT / "data" / "evidence_objects"
    replay_dir: Path = ROOT / "replay" / "cases"
    # Production publishes the expensive overview projection as an atomic data
    # artifact from a separate systemd process.  Local development and unit
    # tests leave this unset and retain the lightweight in-process fallback.
    overview_snapshot_path: Path | None = None
    demo_mode: str = "RECENT_CAPTURE"
    admin_token: str | None = None
    reviewer_token: str | None = None
    reviewer_principals: tuple[tuple[str, str, str], ...] = ()
    operator_token: str | None = None
    api_base_url: str = "http://127.0.0.1:8000"
    web_base_url: str = "http://127.0.0.1:8501"
    api_rate_limit_per_minute: int = 180
    api_rate_limit_max_clients: int = 4096
    api_trusted_proxy_hosts: tuple[str, ...] = ("127.0.0.1", "::1")
    evidence_llm_url: str | None = None
    evidence_llm_model: str = "qwen2.5-0.5b-instruct-q4_k_m"
    evidence_llm_timeout_seconds: float = 30.0
    evidence_llm_max_tokens: int = 900
    capture_llm_enabled: bool = False
    capture_llm_provider: str = "disabled"
    capture_llm_model: str = ""
    capture_llm_base_url: str = ""
    capture_llm_timeout_seconds: float = 45.0
    capture_llm_max_tokens: int = 700
    capture_llm_daily_usd_cap: float = 0.0
    capture_llm_daily_cny_cap: float = 0.0
    # A zero daily cap means unlimited. The worker still has bounded batches,
    # leases, retry counts, timeouts and output tokens.
    capture_llm_daily_request_cap: int = 0

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
            overview_snapshot_path=(
                Path(os.environ["FINANCE_RADAR_OVERVIEW_SNAPSHOT_PATH"]).resolve()
                if os.getenv("FINANCE_RADAR_OVERVIEW_SNAPSHOT_PATH")
                else None
            ),
            demo_mode=os.getenv("FINANCE_RADAR_DEMO_MODE", "RECENT_CAPTURE").upper(),
            admin_token=os.getenv("FINANCE_RADAR_ADMIN_TOKEN") or None,
            reviewer_token=os.getenv("FINANCE_RADAR_REVIEWER_TOKEN") or None,
            reviewer_principals=_reviewer_principals_from_env(),
            operator_token=os.getenv("FINANCE_RADAR_OPERATOR_TOKEN") or None,
            api_base_url=os.getenv("FINANCE_RADAR_API_URL", "http://127.0.0.1:8000").rstrip("/"),
            web_base_url=os.getenv("FINANCE_RADAR_WEB_URL", "http://127.0.0.1:8501").rstrip("/"),
            api_rate_limit_per_minute=max(
                0,
                int(os.getenv("FINANCE_RADAR_API_RATE_LIMIT_PER_MINUTE", "180")),
            ),
            api_rate_limit_max_clients=max(
                1,
                int(os.getenv("FINANCE_RADAR_API_RATE_LIMIT_MAX_CLIENTS", "4096")),
            ),
            api_trusted_proxy_hosts=tuple(
                host.strip()
                for host in os.getenv(
                    "FINANCE_RADAR_API_TRUSTED_PROXY_HOSTS",
                    "127.0.0.1,::1",
                ).split(",")
                if host.strip()
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
            capture_llm_enabled=_env_flag("FINANCE_RADAR_CAPTURE_LLM_ENABLED", False),
            capture_llm_provider=os.getenv(
                "FINANCE_RADAR_CAPTURE_LLM_PROVIDER", "disabled"
            ).strip().lower(),
            capture_llm_model=os.getenv("FINANCE_RADAR_CAPTURE_LLM_MODEL", "").strip(),
            capture_llm_base_url=os.getenv(
                "FINANCE_RADAR_CAPTURE_LLM_BASE_URL", ""
            ).strip(),
            capture_llm_timeout_seconds=max(
                1.0,
                float(os.getenv("FINANCE_RADAR_CAPTURE_LLM_TIMEOUT_SECONDS", "45")),
            ),
            capture_llm_max_tokens=max(
                128,
                min(
                    1200,
                    int(os.getenv("FINANCE_RADAR_CAPTURE_LLM_MAX_TOKENS", "700")),
                ),
            ),
            capture_llm_daily_usd_cap=max(
                0.0,
                float(os.getenv("FINANCE_RADAR_CAPTURE_LLM_DAILY_USD_CAP", "0")),
            ),
            capture_llm_daily_cny_cap=max(
                0.0,
                float(os.getenv("FINANCE_RADAR_CAPTURE_LLM_DAILY_CNY_CAP", "0")),
            ),
            capture_llm_daily_request_cap=max(
                0,
                int(os.getenv("FINANCE_RADAR_CAPTURE_LLM_DAILY_REQUEST_CAP", "0")),
            ),
        )

    @property
    def model_artifact(self) -> Path:
        return self.artifact_dir / "risk_router.joblib"

    @property
    def model_card(self) -> Path:
        return self.artifact_dir / "risk_router_model_card.json"
