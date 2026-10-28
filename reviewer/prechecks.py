"""Verificações determinísticas (sem modelo): viram pistas para o revisor confirmar.

Tudo roda sobre objetos do git num SHA fixo (somente leitura). As migrations são lidas em ordem e reduzidas ao
ESTADO FINAL (última definição de cada função, policy e GRANT), porque migrations novas desfazem as antigas.
Nenhuma pista carrega o valor de um segredo: só tipo e localização.
"""
import base64
import json
import re
from dataclasses import dataclass

from . import gitrepo
from .config import Config

# Nomes específicos do projeto (marcadores de autenticação, checagens de papel, filtro de cliente, tabelas sensíveis)
# vêm do perfil: profiles/<nome>/perfil.toml.
ROLE_GUARD = re.compile(r"\b((?:is|has|require|assert|check)\w*Role\w*)\b")  # + role_checks do perfil


@dataclass(frozen=True)
class Hint:
    rule: str
    file: str
    line: int | None
    message: str

    def text(self) -> str:
        loc = f"{self.file}:{self.line}" if self.line else self.file
        return f"[{self.rule}] {loc} — {self.message}"


# ---------------------------------------------------------------- utilidades git

def _show(cfg: Config, repo, sha: str, path: str) -> str:
    return gitrepo._git(cfg, repo, "show", f"{sha}:{path}", ok=(0, 128))


def _grep(cfg: Config, repo, sha: str, pattern: str, *paths: str, ignore_case: bool = False) -> list[tuple[str, int, str]]:
    args = ["grep", "-n", "-I", "--no-color", "-E"] + (["-i"] if ignore_case else []) + ["-e", pattern, sha, "--", *paths]
    out = gitrepo._git(cfg, repo, *args, ok=(0, 1))
    hits = []
    for line in out.splitlines():
        rest = line[len(sha) + 1:] if line.startswith(f"{sha}:") else line
        path, _, tail = rest.partition(":")
        num, _, text = tail.partition(":")
        if num.isdigit():
            hits.append((path, int(num), text))
    return hits


def _ls(cfg: Config, repo, sha: str, prefix: str) -> list[str]:
    out = gitrepo._git(cfg, repo, "ls-tree", "-r", "--name-only", sha, "--", prefix, ok=(0, 128))
    return out.splitlines()


# ---------------------------------------------------------------- Edge Functions

def _verify_jwt_map(cfg: Config, repo, sha: str) -> dict[str, bool]:
    text = _show(cfg, repo, sha, "supabase/config.toml")
    result, current = {}, None
    for raw in text.splitlines():
        line = raw.strip()
        m = re.match(r"\[functions\.([\w-]+)\]", line)
        if m:
            current = m.group(1)
            continue
        if line.startswith("["):
            current = None
        m = re.match(r"verify_jwt\s*=\s*(true|false)", line)
        if current and m:
            result[current] = m.group(1) == "true"
    return result


def edge_inventory(cfg: Config, repo, sha: str) -> list[dict]:
    """Uma linha por Edge Function: quem pode chamar e que proteções o código mostra."""
    p = cfg.profile
    jwt = _verify_jwt_map(cfg, repo, sha)
    rows = []
    for path in _ls(cfg, repo, sha, "supabase/functions"):
        m = re.fullmatch(r"supabase/functions/([\w-]+)/index\.ts", path)
        if not m or m.group(1).startswith("_"):
            continue
        name, code = m.group(1), _show(cfg, repo, sha, path)
        rows.append({
            "name": name, "file": path,
            "verify_jwt": jwt.get(name),  # None = sem bloco no config.toml
            "auth": [mk for mk in p.auth_markers if mk in code],
            "internal": any(mk in code for mk in p.internal_markers),
            "service_role": "SERVICE_ROLE" in code,
            "tenant_filter": bool(p.tenant_filter.search(code)),
            "role_guard": sorted(set(ROLE_GUARD.findall(code)) | {rc for rc in p.role_checks if rc in code}),
            "reads_sensitive": any(t in code for t in p.sensitive_tables),
            "audit": sorted(m for m in p.audit_markers if m in code),
            "external_fetch": bool(re.search(r"fetch\(\s*(?![`'\"]https://)", code)),
            "safe_url": any(m in code for m in p.safe_fetch_markers),
            "code": code,
        })
    return rows


def _edge_hints(rows: list[dict], p) -> list[Hint]:
    hints = []
    for r in rows:
        if r["verify_jwt"] is False and not r["auth"]:
            hints.append(Hint("EF-2", r["file"], None,
                              "verify_jwt=false no config.toml e nenhum marcador de autenticação no index.ts "
                              f"({', '.join(p.auth_markers[:4])}...): confirmar onde o chamador é validado"))
        if r["verify_jwt"] is None:
            hints.append(Hint("EF-2", r["file"], None, "função sem bloco [functions.<nome>] no config.toml "
                              "(vale o padrão verify_jwt=true; conferir se o deploy usa --no-verify-jwt)"))
        if r["service_role"] and not r["tenant_filter"] and not r["internal"]:  # cron/interna processa todos
            hints.append(Hint("DB-2", r["file"], None,
                              f"usa service role (ignora RLS) e não menciona o filtro de cliente ({p.tenant_filter.pattern})"))
        if r["external_fetch"] and not r["safe_url"]:
            for i, line in enumerate(r["code"].splitlines(), 1):
                if re.search(r"fetch\(\s*(?![`'\"]https://)", line):
                    hints.append(Hint("EF-9", r["file"], i, "fetch com URL não literal e sem validação de URL no arquivo "
                                      f"({', '.join(p.safe_fetch_markers) or 'nenhum validador configurado'})"))
                    break
    return hints


# ---------------------------------------------------------------- SQL: estado final das migrations

_DOLLAR = re.compile(r"\$([A-Za-z_]*)\$")


def _statements(sql: str) -> list[tuple[int, str]]:
    """Divide SQL em comandos respeitando $$corpos$$, 'strings' e comentarios --. Retorna (linha, texto)."""
    out, buf, start, line, i, n = [], [], 1, 1, 0, len(sql)
    while i < n:
        ch = sql[i]
        if not buf and ch.isspace():
            line += ch == "\n"
            i += 1
            start = line
            continue
        if sql.startswith("--", i):
            end = sql.find("\n", i)
            end = n if end == -1 else end
            i = end
            continue
        if ch == "'":
            end = i + 1
            while end < n and not (sql[end] == "'" and not sql.startswith("''", end)):
                end += 2 if sql.startswith("''", end) else 1
            chunk = sql[i:end + 1]
        else:
            m = _DOLLAR.match(sql, i) if ch == "$" else None
            if m:
                tag = m.group(0)
                end = sql.find(tag, i + len(tag))
                end = n if end == -1 else end + len(tag) - 1
                chunk = sql[i:end + 1]
            elif ch == ";":
                text = "".join(buf).strip()
                if text:
                    out.append((start, text))
                buf, i = [], i + 1
                continue
            else:
                chunk = ch
                end = i
        buf.append(chunk)
        line += chunk.count("\n")
        i = end + 1
    if "".join(buf).strip():
        out.append((start, "".join(buf).strip()))
    return out


def _name(raw: str) -> str:
    name = raw.replace('"', "").lower()
    return name if "." in name else f"public.{name}"


CLIENT_ROLES = {"public", "anon", "authenticated"}


def _roles(text: str) -> set[str]:
    return {r.strip().split()[0].strip('"') for r in text.split(",") if r.strip()}


def sql_state(cfg: Config, repo, sha: str) -> dict:
    """Reduz as migrations (em ordem) ao estado final de funções, policies, GRANTs para anon/public e de quem
    pode executar cada função (CREATE OR REPLACE mantém os privilégios, como no Postgres)."""
    cutoff = cfg.profile.no_default_execute_since  # migration a partir da qual função nova nasce sem EXECUTE p/ PUBLIC
    funcs: dict[str, dict] = {}
    exec_roles: dict[str, set[str]] = {}
    policies: dict[tuple, dict] = {}
    grants: dict[tuple, dict] = {}
    for path in sorted(p for p in _ls(cfg, repo, sha, "supabase/migrations") if p.endswith(".sql")):
        for line, stmt in _statements(_show(cfg, repo, sha, path)):
            low = stmt.lower()
            m = re.match(r"create\s+(?:or\s+replace\s+)?function\s+([\w.\"]+)\s*\(", low)
            if m:
                fn = _name(m.group(1))
                funcs[fn] = {"file": path, "line": line, "text": stmt}
                if fn not in exec_roles:
                    newer = bool(cutoff) and path.split("/")[-1] >= cutoff
                    exec_roles[fn] = set() if newer else {"public"}
                continue
            m = re.match(r"drop\s+function\s+(?:if\s+exists\s+)?([\w.\"]+)", low)
            if m:
                funcs.pop(_name(m.group(1)), None)
                exec_roles.pop(_name(m.group(1)), None)
                continue
            if low.startswith("do") and "format(" in low:
                # Revogação em massa por SQL dinâmico, num laço sobre o catálogo:
                # EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC, anon', ...). Vale para tudo que já existe.
                for m in re.finditer(r"revoke\s+(?:all|execute)\s+on\s+function\s+%s\s+from\s+([\w\s,]+?)'", low):
                    for roles in exec_roles.values():
                        roles.difference_update(_roles(m.group(1)) & {"public", "anon"})
                for m in re.finditer(r"revoke\s+all\s+on\s+(?:table|sequence)\s+%s\s+from\s+([\w\s,]+?)'", low):
                    for key in [k for k in grants if k[1] in _roles(m.group(1))]:
                        grants.pop(key)
                continue
            m = re.match(r"(grant|revoke)\s+(?:all|execute)(?:\s+privileges)?\s+on\s+(all\s+functions\s+in\s+schema\s+\w+|"
                         r"function\s+([\w.\"]+))[^;]*?\s+(to|from)\s+(.+)", low, re.S)
            if m:
                targets = list(exec_roles) if m.group(2).startswith("all") else [_name(m.group(3))]
                for fn in targets:
                    roles = exec_roles.setdefault(fn, set())
                    (roles.update if m.group(1) == "grant" else roles.difference_update)(_roles(m.group(5)))
                continue
            m = re.match(r"create\s+policy\s+(\"[^\"]+\"|\w+)\s+on\s+([\w.\"]+)(.*)", low, re.S)
            if m:
                to = re.search(r"\bto\s+([\w\s,]+?)(?:\s+using\b|\s+with\s+check\b|$)", m.group(3), re.S)
                roles = [r.strip() for r in to.group(1).split(",")] if to else []  # sem TO: padrão do Postgres
                policies[(m.group(1).strip('"'), _name(m.group(2)))] = {"file": path, "line": line, "roles": roles}
                continue
            m = re.match(r"drop\s+policy\s+(?:if\s+exists\s+)?(\"[^\"]+\"|\w+)\s+on\s+([\w.\"]+)", low)
            if m:
                policies.pop((m.group(1).strip('"'), _name(m.group(2))), None)
                continue
            m = re.match(r"(grant|revoke)\s+(.+?)\s+on\s+(.+?)\s+(to|from)\s+(.+)", low, re.S)
            if m:
                roles = {r.strip().split()[0] for r in m.group(5).split(",") if r.strip()}
                for role in roles & {"anon", "public"}:
                    key = (" ".join(m.group(3).split()), role)
                    if m.group(1) == "grant":
                        grants[key] = {"file": path, "line": line, "privs": " ".join(m.group(2).split())}
                    else:
                        grants.pop(key, None)
    for fn, f in funcs.items():
        f["exec"] = sorted(exec_roles.get(fn, set()))
    return {"functions": funcs, "policies": policies, "grants": grants}


def _sql_hints(state: dict, p) -> list[Hint]:
    hints = []
    for (obj, role), g in state["grants"].items():
        if role == "anon" or not g["privs"].startswith("execute"):
            hints.append(Hint("SQL-1", g["file"], g["line"], f"GRANT {g['privs']} ON {obj} TO {role} continua valendo no estado final"))
    for (pol, table), info in state["policies"].items():
        bad = [r for r in info["roles"] if r.startswith(("public", "anon"))]
        if bad:
            hints.append(Hint("SQL-1", info["file"], info["line"],
                              f"policy '{pol}' em {table} vale para {', '.join(bad)} no estado final"))
    for name, f in state["functions"].items():
        low = f["text"].lower()
        if "security definer" not in low or re.search(r"returns\s+(setof\s+)?trigger", low):
            continue
        callers = set(f["exec"]) & CLIENT_ROLES
        if not callers:  # só service_role/postgres executam: não é porta de entrada do app
            continue
        who = ", ".join(sorted(callers))
        if callers & {"public", "anon"}:
            hints.append(Hint("SQL-1", f["file"], f["line"], f"{name} é SECURITY DEFINER e executável por {who}"))
        if "search_path" not in low:
            hints.append(Hint("SQL-2", f["file"], f["line"], f"{name} é SECURITY DEFINER sem search_path fixo"))
        writes = re.search(r"\b(insert\s+into|update\s+[\w.\"]+\s+set|delete\s+from)\b", low)
        if writes and not any(rc.lower() in low for rc in p.role_checks):
            hints.append(Hint("SQL-2", f["file"], f["line"],
                              f"{name} é SECURITY DEFINER, executável por {who}, escreve no banco e não confere papel"))
    return hints


# ---------------------------------------------------------------- segredos, dados pessoais, front-end

SECRET_PATTERNS = [
    ("chave da Anthropic", r"sk-ant-[A-Za-z0-9_-]{20,}"),
    ("chave da OpenAI", r"sk-(proj-)?[A-Za-z0-9]{32,}"),
    ("chave de acesso AWS", r"AKIA[0-9A-Z]{16}"),
    ("token do Slack", r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    ("chave privada", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("token de bot do Telegram", r"[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}"),
    ("chave de API do Google", r"AIza[0-9A-Za-z_-]{35}"),
]
JWT = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.([A-Za-z0-9_-]{10,})\.[A-Za-z0-9_-]{10,}")
SKIP_SECRET_FILES = re.compile(r"(package-lock\.json|deno\.lock|yarn\.lock|pnpm-lock\.yaml|\.env\.example)$")


PEM_HANDLING = re.compile(r"\.(replace|includes|startsWith|endsWith|indexOf|split|match)\(|===?|!==?")
TEST_FILE = re.compile(r"(^|/)(__tests__|tests?)/|\.(test|spec)\.[jt]sx?$")


def _jwt_role(token_payload: str) -> str | None:
    try:
        padded = token_payload + "=" * (-len(token_payload) % 4)
        return json.loads(base64.urlsafe_b64decode(padded)).get("role")
    except Exception:
        return None


def _secret_hints(cfg: Config, repo, sha: str) -> list[Hint]:
    hints = []
    for label, pattern in SECRET_PATTERNS:
        for path, line, text in _grep(cfg, repo, sha, pattern, "."):
            if SKIP_SECRET_FILES.search(path) or path.endswith(".md"):
                continue
            if label == "chave privada" and PEM_HANDLING.search(text):
                continue  # código que procura/remove o cabeçalho PEM, não uma chave
            note = " (arquivo de teste: provavelmente fictício, confirmar)" if TEST_FILE.search(path) else ""
            hints.append(Hint("EF-6", path, line, f"possível {label} no código (valor omitido){note}"))
    for path, line, text in _grep(cfg, repo, sha, r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.", "."):
        for m in JWT.finditer(text):
            if _jwt_role(m.group(1)) == "service_role":  # a chave anon no front é pública e esperada
                hints.append(Hint("EF-6", path, line, "JWT com role=service_role no código (valor omitido)"))
    return hints


PII_LOG = (r"console\.(log|info|warn|error|debug)\(.*\b(cpf|cns|phone|telefone|nascimento|birth_?date|receita|"
           r"patient_?name|nome_?paciente)\b")


def _pii_and_front_hints(cfg: Config, repo, sha: str) -> list[Hint]:
    hints = []
    for path, line, _ in _grep(cfg, repo, sha, PII_LOG, "supabase/functions", "src", ignore_case=True):
        rule = "EF-7" if path.startswith("supabase/") else "FE-7"
        hints.append(Hint(rule, path, line, "log pode conter dado pessoal"))
    for path, line, _ in _grep(cfg, repo, sha, r"(local|session)Storage\.setItem\(.*(patient|paciente|cpf|cns|token|senha|password)",
                               "src", ignore_case=True):
        hints.append(Hint("FE-7", path, line, "grava dado sensível no armazenamento do navegador"))
    for path, line, _ in _grep(cfg, repo, sha, r"dangerouslySetInnerHTML|\.innerHTML\s*=", "src"):
        hints.append(Hint("FE-10", path, line, "HTML injetado sem passar pelo React: conferir se o conteúdo é sanitizado"))
    return hints


# ---------------------------------------------------------------- execução

def run(cfg: Config, repo, sha: str) -> tuple[list[Hint], dict]:
    """Retorna (pistas, contexto). O contexto (inventário e estado SQL) é reaproveitado pelos temas."""
    edge = edge_inventory(cfg, repo, sha)
    state = sql_state(cfg, repo, sha)
    hints = (_edge_hints(edge, cfg.profile) + _sql_hints(state, cfg.profile) + _secret_hints(cfg, repo, sha)
             + _pii_and_front_hints(cfg, repo, sha))
    return hints, {"edge": edge, "sql": state}


def for_files(hints: list[Hint], files: set[str], limit: int = 25) -> list[str]:
    return [h.text() for h in hints if h.file in files][:limit]


def for_rules(hints: list[Hint], prefixes: set[str] | None, limit: int = 25) -> list[str]:
    picked = [h for h in hints if prefixes is None or h.rule.split("-")[0] in prefixes]
    return [h.text() for h in picked][:limit]
