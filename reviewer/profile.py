"""Perfil de projeto: tudo que é específico do código revisado fica numa pasta profiles/<nome>/.

    profiles/<nome>/
    ├── perfil.toml   nome do projeto e parâmetros das verificações automáticas
    ├── contexto.md   o que o sistema faz, quem usa, o que é dado sensível (vai para os prompts)
    ├── regras.md     checklist de revisão com IDs (EF-1, SQL-2...), em seções "#### PREFIXO — título"
    ├── exemplos.md   exemplos de achados no formato da ferramenta (opcional)
    └── casos.json    falhas reais já corrigidas, para o benchmark (opcional)

O perfil "exemplo" vem com o repositório. Perfis próprios ficam fora do git (veja .gitignore): regras e casos de um
sistema real descrevem o funcionamento interno dele e não devem ser publicados.
"""
import re
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROFILES_DIR = Path(__file__).resolve().parent.parent / "profiles"

DEFAULTS = {
    "auth_markers": ["auth.getUser", "getClaims", "getUser(", "x-cron-secret"],
    "internal_markers": ["x-cron-secret"],
    "role_checks": [],
    "tenant_filter": r"tenant_id|organization_id|org_id",
    "no_default_execute_since": "",
    "sensitive_tables": [],
    "sensitive_label": "dados sensíveis",
    "audit_markers": [],
    "safe_fetch_markers": ["assertSafeUrl"],
}


class ProfileError(SystemExit):
    pass


@dataclass(frozen=True)
class Profile:
    name: str
    path: Path
    project: str
    context: str
    rules: str
    examples: str
    auth_markers: tuple[str, ...]
    internal_markers: tuple[str, ...]
    role_checks: tuple[str, ...]
    tenant_filter: re.Pattern
    no_default_execute_since: str
    sensitive_tables: tuple[str, ...]
    sensitive_label: str
    audit_markers: tuple[str, ...]
    safe_fetch_markers: tuple[str, ...]

    @property
    def cases_path(self) -> Path:
        return self.path / "casos.json"


def available() -> list[str]:
    if not PROFILES_DIR.is_dir():
        return []
    return sorted(p.name for p in PROFILES_DIR.iterdir() if (p / "perfil.toml").is_file())


def _read(path: Path, required: bool) -> str:
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    if required:
        raise ProfileError(f"Perfil incompleto: falta {path}")
    return ""


@lru_cache(maxsize=None)
def load(name_or_path: str) -> Profile:
    """Carrega pelo nome (pasta em profiles/) ou por um caminho de pasta."""
    path = Path(name_or_path) if "/" in name_or_path else PROFILES_DIR / name_or_path
    toml = path / "perfil.toml"
    if not toml.is_file():
        options = ", ".join(available()) or "nenhum"
        raise ProfileError(f"Perfil '{name_or_path}' não encontrado (procurei {toml}). Disponíveis: {options}. "
                           "Escolha outro em PROFILE no .env ou rode `check configurar`.")
    try:
        data = tomllib.loads(toml.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ProfileError(f"{toml} tem um erro de formato: {exc}") from exc
    pre = {**DEFAULTS, **data.get("prechecks", {})}
    try:
        tenant = re.compile(pre["tenant_filter"])
    except re.error as exc:
        raise ProfileError(f"tenant_filter inválido em {toml}: {exc}") from exc
    return Profile(
        name=path.name, path=path, project=data.get("nome", path.name),
        context=_read(path / "contexto.md", required=True),
        rules=_read(path / "regras.md", required=True),
        examples=_read(path / "exemplos.md", required=False),
        auth_markers=tuple(pre["auth_markers"]), internal_markers=tuple(pre["internal_markers"]), role_checks=tuple(pre["role_checks"]),
        tenant_filter=tenant, no_default_execute_since=str(pre["no_default_execute_since"]),
        sensitive_tables=tuple(pre["sensitive_tables"]), sensitive_label=pre["sensitive_label"],
        audit_markers=tuple(pre["audit_markers"]), safe_fetch_markers=tuple(pre["safe_fetch_markers"]),
    )
