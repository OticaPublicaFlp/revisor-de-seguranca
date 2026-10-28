"""Divide o código da branch em unidades revisáveis e escolhe quais revisar agora.

O agente lê o código direto da branch (objetos do git, somente leitura). Cada unidade tem uma
impressão digital (blobs do git): só é revisada de novo se o código dela mudar ou se passar de
`recheck_days` sem revisão. Assim a cobertura da base inteira avança aos poucos, sem repetir custo.
"""
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from . import gitrepo
from .config import Config
from .tools import DENY

EXTENSIONS = (".ts", ".tsx", ".sql", ".toml", ".yml", ".yaml")
MAX_FILE_BYTES = 150_000
MIGRATION_BATCH = 15  # migrations por unidade; lotes em ordem crescente mantêm as chaves estáveis
EXCLUDE = re.compile(
    r"\.(test|spec)\.[jt]sx?$|(^|/)(tests?|__tests__|__mocks__|coverage)/|\.d\.ts$"
    r"|(^|/)supabase/types|(^|/)integrations/supabase/types\.ts$"
)


@dataclass(frozen=True)
class Unit:
    key: str
    files: tuple  # (caminho, tamanho, blob)
    prio: int
    order: int = 0
    kind: str = "code"  # "code" (arquivos) ou "theme" (revisão transversal sobre um inventário)
    inventory: str = ""
    instruction: str = ""
    groups: frozenset | None = None  # só para temas; unidades de código usam rule_groups(key)

    @property
    def fingerprint(self) -> str:
        raw = self.inventory or "\n".join(f"{p}:{blob}" for p, _, blob in self.files)
        return hashlib.sha1(raw.encode()).hexdigest()

    @property
    def chars(self) -> int:
        return len(self.inventory) if self.inventory else sum(size for _, size, _ in self.files)

    @property
    def paths(self) -> set[str]:
        return {p for p, _, _ in self.files}


def _group_of(path: str) -> str:
    parts = path.split("/")
    if path.startswith("supabase/functions/") and len(parts) >= 4:
        return "/".join(parts[:3])  # supabase/functions/<nome> e _shared
    if path.startswith("supabase/migrations/"):
        return "supabase/migrations"
    if parts[0] == "src" and len(parts) >= 4 and parts[1] in ("components", "lib"):
        return "/".join(parts[:3])
    if len(parts) == 1:
        return "(raiz)"
    return "/".join(parts[:2])


def rule_groups(key: str) -> frozenset[str] | None:
    """Grupos de regras (prefixos do rules.md) que se aplicam à unidade. None = todos."""
    if key.startswith("tema:"):
        from .themes import THEMES
        return THEMES[key.split(":", 1)[1]][1]
    if key.startswith("supabase/functions"):
        return frozenset({"EF", "DB", "REL", "AI", "CX"})
    if key.startswith("supabase/migrations"):
        return frozenset({"SQL", "DB"})
    if key.startswith("src"):
        return frozenset({"FE", "DB"})
    return None


def _prio(group: str) -> int:
    """Menor = revisado antes: o que mexe com autenticação e dados vem primeiro."""
    if group.startswith("supabase/functions"):
        return 0
    if group.startswith("supabase/migrations"):
        return 1
    if group.startswith((".github", "src/lib")):
        return 2
    if group.startswith("src"):
        return 3
    return 4


def src_files(cfg: Config, repo, head: str) -> list[str]:
    return [p for p, _, _ in _listing(cfg, repo, head) if p.startswith("src/")]


def _listing(cfg: Config, repo, head: str) -> list[tuple[str, int, str]]:
    out = gitrepo._git(cfg, repo, "ls-tree", "-r", "-l", "-z", head)
    files = []
    for entry in out.split("\0"):
        meta, _, path = entry.partition("\t")
        if not path:
            continue
        _mode, kind, blob, size = meta.split(None, 3)
        size = int(size) if size.strip() != "-" else 0
        if (kind != "blob" or not path.endswith(EXTENSIONS) or size > MAX_FILE_BYTES or size == 0
                or gitrepo.IGNORED.search(path) or DENY.search(path) or EXCLUDE.search(path)):
            continue
        files.append((path, size, blob))
    return files


def build(cfg: Config, repo, head: str) -> list[Unit]:
    groups: dict[str, list] = {}
    for f in sorted(_listing(cfg, repo, head)):
        groups.setdefault(_group_of(f[0]), []).append(f)

    units: list[Unit] = []
    for group, files in groups.items():
        batches, cur, size = [], [], 0
        for f in files:
            over_budget = cur and size + f[1] > cfg.chunk_chars
            over_count = group == "supabase/migrations" and len(cur) >= MIGRATION_BATCH
            if over_budget or over_count:
                batches.append(cur)
                cur, size = [], 0
            cur.append(f)
            size += f[1]
        if cur:
            batches.append(cur)
        for i, batch in enumerate(batches):
            key = group if len(batches) == 1 else f"{group}#{i + 1}"
            # migrations: o lote mais novo (maior índice) é revisado primeiro
            order = -i if group == "supabase/migrations" else i
            units.append(Unit(key, tuple(batch), _prio(group), order))
    return units


def select(reviewed: dict[str, tuple[str, str]], units: list[Unit], cfg: Config,
           now: datetime | None = None) -> list[tuple[Unit, str]]:
    """Escolhe até `max_units`: alteradas, depois nunca revisadas, depois as mais antigas."""
    now = now or datetime.now(timezone.utc)
    ranked = []
    for u in units:
        state = reviewed.get(u.key)
        if state is None:
            ranked.append(((1, u.prio, u.order, u.key), u, "nunca revisada"))
        elif state[0] != u.fingerprint:
            ranked.append(((0, u.prio, u.order, u.key), u, "código alterado desde a última revisão"))
        else:
            age = (now - datetime.fromisoformat(state[1])).days
            if age >= cfg.recheck_days:
                ranked.append(((2, -age, u.prio, u.key), u, f"última revisão há {age} dias"))
    ranked.sort(key=lambda r: r[0])
    return [(u, reason) for _, u, reason in ranked[: cfg.max_units]]


def status(reviewed: dict[str, tuple[str, str]], u: Unit) -> str:
    state = reviewed.get(u.key)
    if state is None:
        return "nunca revisada"
    return "em dia" if state[0] == u.fingerprint else "alterada"


def render(cfg: Config, repo, head: str, unit: Unit) -> str:
    """Conteúdo da unidade com linhas numeradas (para o campo `line` dos achados bater)."""
    if unit.kind == "theme":
        return unit.inventory
    parts = []
    for path, _size, _blob in unit.files:
        text = gitrepo._git(cfg, repo, "show", f"{head}:{path}")
        lines = text.splitlines()
        body = "\n".join(f"{i:>5}  {line}" for i, line in enumerate(lines, 1))
        if len(body) > cfg.chunk_chars:
            body = body[: cfg.chunk_chars] + "\n[... arquivo truncado; use read_file(start_line) para o restante ...]"
        parts.append(f"<arquivo caminho={path!r} linhas={len(lines)}>\n{body}\n</arquivo>")
    return "\n\n".join(parts)
