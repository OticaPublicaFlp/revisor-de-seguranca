import os
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Configuração incompleta: falta {name}. Rode `check configurar` (ou veja .env.example).")
    return value


@dataclass(frozen=True)
class Config:
    repo_url: str
    repo_branch: str
    repo_token: str
    cron_expr: str
    tz: str
    initial_lookback: int
    anthropic_api_key: str
    model: str
    max_chunks: int
    chunk_chars: int
    max_output_tokens: int
    max_turns: int
    verify: bool
    telegram_token: str
    telegram_chat_id: int
    telegram_approver_ids: frozenset[int]
    slack_token: str
    slack_channel: str
    slack_include_details: bool
    data_dir: Path
    repo_ssh_key: str = ""
    mode: str = "code"
    max_units: int = 5
    recheck_days: int = 30
    effort: str = "medium"
    dual_review: str = "critical"
    deps_check: bool = True
    profile_name: str = "exemplo"

    @cached_property
    def profile(self):
        """Perfil do projeto revisado (profiles/<nome>/): contexto, regras, exemplos e parâmetros das pistas."""
        from .profile import load
        return load(self.profile_name)


def _approvers() -> frozenset[int]:
    """IDs autorizados a aprovar. Vazio = qualquer membro do chat configurado pode aprovar."""
    raw = os.environ.get("TELEGRAM_APPROVER_IDS", "").strip()
    return frozenset(int(x) for x in raw.replace(";", ",").split(",") if x.strip())


def _choice(name: str, default: str, allowed: tuple[str, ...]) -> str:
    value = os.environ.get(name, default).strip().lower()
    if value not in allowed:
        raise SystemExit(f"{name} deve ser um de: {', '.join(allowed)} (veio {value!r})")
    return value


def _mode() -> str:
    mode = os.environ.get("REVIEW_MODE", "code").strip().lower()
    if mode not in ("code", "diff"):
        raise SystemExit("REVIEW_MODE deve ser 'code' (revisa o código da branch) ou 'diff' (só o que mudou)")
    return mode


def load_config(need_telegram: bool = True) -> Config:
    load_dotenv(ROOT / ".env")
    data_dir = Path(os.environ.get("DATA_DIR", "./data"))
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    data_dir.mkdir(parents=True, exist_ok=True)

    return Config(
        repo_url=_required("REPO_URL"),
        repo_branch=os.environ.get("REPO_BRANCH", "main"),
        repo_token=os.environ.get("REPO_TOKEN", "").strip(),
        cron_expr=os.environ.get("CRON_EXPR", "0 8 * * *"),
        tz=os.environ.get("TZ", "America/Sao_Paulo"),
        initial_lookback=int(os.environ.get("INITIAL_LOOKBACK_COMMITS", "20")),
        anthropic_api_key=_required("ANTHROPIC_API_KEY"),
        model=os.environ.get("REVIEW_MODEL", "claude-opus-5-5"),
        max_chunks=int(os.environ.get("MAX_CHUNKS", "6")),
        chunk_chars=int(os.environ.get("CHUNK_CHARS", "60000")),
        max_output_tokens=int(os.environ.get("MAX_OUTPUT_TOKENS", "32000")),
        max_turns=int(os.environ.get("MAX_TURNS", "14")),
        verify=os.environ.get("VERIFY", "true").lower() == "true",
        telegram_token=_required("TELEGRAM_BOT_TOKEN") if need_telegram else "",
        telegram_chat_id=int(_required("TELEGRAM_CHAT_ID")) if need_telegram else 0,
        telegram_approver_ids=_approvers() if need_telegram else frozenset(),
        slack_token=os.environ.get("SLACK_BOT_TOKEN", "").strip(),
        slack_channel=os.environ.get("SLACK_CHANNEL_ID", "").strip(),
        slack_include_details=os.environ.get("SLACK_INCLUDE_DETAILS", "false").lower() == "true",
        data_dir=data_dir,
        repo_ssh_key=os.environ.get("REPO_SSH_KEY", "").strip(),
        mode=_mode(),
        max_units=int(os.environ.get("MAX_UNITS", "5")),
        recheck_days=int(os.environ.get("RECHECK_DAYS", "30")),
        effort=_choice("EFFORT", "medium", ("low", "medium", "high", "xhigh", "max")),
        dual_review=_choice("DUAL_REVIEW", "critical", ("off", "critical", "all")),
        deps_check=os.environ.get("DEPS_CHECK", "true").lower() == "true",
        profile_name=os.environ.get("PROFILE", "exemplo").strip() or "exemplo",
    )
