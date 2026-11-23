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
    tables: dict[str, dict] = {}   # tabelas de public: RLS ligada? privilégios padrão de anon revogados?
    views: dict[str, dict] = {}
    buckets: dict[str, dict] = {}  # buckets públicos sem restrição de tipo de arquivo
    anon_default_revoked = False   # ALTER DEFAULT PRIVILEGES ... REVOKE ... ON TABLES FROM anon
    for path in sorted(p for p in _ls(cfg, repo, sha, "supabase/migrations") if p.endswith(".sql")):
        for line, stmt in _statements(_show(cfg, repo, sha, path)):
            low = stmt.lower()
            if _track_objects(low, path, line, tables, views, buckets, anon_default_revoked):
                anon_default_revoked = anon_default_revoked or low.startswith("alter default privileges")
                continue
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
                    if "anon" in _roles(m.group(1)):
                        for t in tables.values():
                            t["anon_revoked"] = True
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
                policies[(m.group(1).strip('"'), _name(m.group(2)))] = {"file": path, "line": line, "roles": roles,
                                                                        "text": m.group(3)}
                continue
            m = re.match(r"drop\s+policy\s+(?:if\s+exists\s+)?(\"[^\"]+\"|\w+)\s+on\s+([\w.\"]+)", low)
            if m:
                policies.pop((m.group(1).strip('"'), _name(m.group(2))), None)
                continue
            m = re.match(r"(grant|revoke)\s+(.+?)\s+on\s+(.+?)\s+(to|from)\s+(.+)", low, re.S)
            if m:
                roles = {r.strip().split()[0] for r in m.group(5).split(",") if r.strip()}
                if m.group(1) == "revoke" and roles & {"anon", "public"}:
                    target = " ".join(m.group(3).split())
                    names = (list(tables) if target.startswith("all tables in schema public") else
                             [_name(n) for n in re.sub(r"^table\s+", "", target).split(",")])
                    for n in names:
                        if n in tables:
                            tables[n]["anon_revoked"] = True
                for role in roles & {"anon", "public"}:
                    key = (" ".join(m.group(3).split()), role)
                    if m.group(1) == "grant":
                        grants[key] = {"file": path, "line": line, "privs": " ".join(m.group(2).split())}
                    else:
                        grants.pop(key, None)
    for fn, f in funcs.items():
        f["exec"] = sorted(exec_roles.get(fn, set()))
    return {"functions": funcs, "policies": policies, "grants": grants, "tables": tables, "views": views,
            "buckets": buckets}


def _track_objects(low: str, path: str, line: int, tables: dict, views: dict, buckets: dict,
                   anon_default_revoked: bool) -> bool:
    """Tabelas, views, buckets e privilégios padrão (base das verificações dos Advisors do Supabase).
    Retorna True se o comando foi consumido aqui."""
    m = re.match(r"create\s+table\s+(?:if\s+not\s+exists\s+)?([\w.\"]+)", low)
    if m:
        name = _name(m.group(1))
        if name.startswith("public.") and name not in tables:
            tables[name] = {"file": path, "line": line, "rls": False, "anon_revoked": anon_default_revoked}
        return True
    m = re.match(r"drop\s+table\s+(?:if\s+exists\s+)?([\w.\"]+)", low)
    if m:
        tables.pop(_name(m.group(1)), None)
        return True
    m = re.match(r"alter\s+table\s+(?:if\s+exists\s+)?(?:only\s+)?([\w.\"]+)\s+(enable|disable)\s+row\s+level\s+security", low)
    if m:
        if _name(m.group(1)) in tables:
            tables[_name(m.group(1))]["rls"] = m.group(2) == "enable"
        return True
    m = re.match(r"create\s+(?:or\s+replace\s+)?view\s+(?:if\s+not\s+exists\s+)?([\w.\"]+)", low)
    if m:
        views[_name(m.group(1))] = {"file": path, "line": line,
                                    "invoker": bool(re.search(r"security_invoker\s*=\s*(true|on|1|yes)", low))}
        return True
    m = re.match(r"alter\s+view\s+(?:if\s+exists\s+)?([\w.\"]+)\s+set\s*\(\s*security_invoker\s*=\s*(true|on|1|yes)", low)
    if m:
        if _name(m.group(1)) in views:
            views[_name(m.group(1))]["invoker"] = True
        return True
    m = re.match(r"drop\s+view\s+(?:if\s+exists\s+)?([\w.\"]+)", low)
    if m:
        views.pop(_name(m.group(1)), None)
        return True
    if re.match(r"alter\s+default\s+privileges\b.*\brevoke\b.*\bon\s+tables\b.*\bfrom\b.*\banon\b", low, re.S):
        return True
    if "storage.buckets" in low:
        ids = re.findall(r"'([\w.-]+)'", low)
        if low.startswith("insert") and re.search(r"\btrue\b", low) and "allowed_mime_types" not in low and ids:
            buckets[ids[0]] = {"file": path, "line": line}
        elif "allowed_mime_types" in low or re.search(r"public\s*=\s*false", low):
            for i in ids:
                buckets.pop(i, None)
        return low.startswith(("insert", "update"))
    return False


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
        if re.search(r"\bexecute\b(?!\s+(?:procedure|function)\b)", low):
            hints.append(Hint("SQL-2", f["file"], f["line"],
                              f"{name} é SECURITY DEFINER, executável por {who} e usa EXECUTE dinâmico: "
                              "conferir risco de injeção SQL ou escalada"))
    hints += _advisor_hints(state)
    return hints


ALWAYS_TRUE = re.compile(r"\b(using|with\s+check)\s*\(\s*\(?\s*(true|1\s*=\s*1)\s*\)?\s*\)")


def _advisor_hints(state: dict) -> list[Hint]:
    """Versão estática (lendo as migrations) das verificações de segurança dos Database Advisors do Supabase."""
    hints = []
    for name, t in state["tables"].items():
        if not t["rls"]:
            hints.append(Hint("SQL-1", t["file"], t["line"], f"{name} fica sem RLS no estado final "
                              "(advisor rls_disabled_in_public): pela API, quem tem GRANT lê e altera tudo"))
    by_file: dict[str, list[tuple[int, str]]] = {}  # uma pista por migration, não uma por tabela
    for name, t in state["tables"].items():
        if not t["anon_revoked"]:
            by_file.setdefault(t["file"], []).append((t["line"], name.removeprefix("public.")))
    for path, items in by_file.items():
        names = ", ".join(n for _, n in items[:8]) + (f" e mais {len(items) - 8}" if len(items) > 8 else "")
        hints.append(Hint("SQL-1", path, min(line for line, _ in items), f"nenhuma migration revoga de anon os "
                          f"privilégios padrão de {names} (o Supabase concede SELECT/INSERT/UPDATE/DELETE/TRUNCATE; "
                          "TRUNCATE ignora a RLS)"))
    for (pol, table), info in state["policies"].items():
        text = info["text"]
        if re.search(r"user_metadata|raw_user_meta_data", text):
            hints.append(Hint("SQL-10", info["file"], info["line"], f"policy '{pol}' em {table} usa user_metadata, que "
                              "o próprio usuário edita (advisor rls_references_user_metadata)"))
        cmd = re.search(r"\bfor\s+(all|select|insert|update|delete)\b", text)
        for_clients = not info["roles"] or any(r.split()[0] in CLIENT_ROLES for r in info["roles"])  # sem TO = todos
        if (for_clients and "as restrictive" not in text and (cmd.group(1) if cmd else "all") != "select"
                and ALWAYS_TRUE.search(text)):
            hints.append(Hint("SQL-11", info["file"], info["line"], f"policy de escrita '{pol}' em {table} tem condição "
                              "sempre verdadeira (advisor rls_policy_always_true)"))
        if for_clients and (cmd.group(1) if cmd else "") == "update" and "with check" not in text:
            hints.append(Hint("SQL-2", info["file"], info["line"],
                              f"policy de UPDATE '{pol}' em {table} não tem WITH CHECK: conferir se o usuário "
                              "consegue alterar colunas sensíveis (role, organization_id, is_active)"))
    for name, v in state["views"].items():
        if not v["invoker"]:
            hints.append(Hint("SQL-12", v["file"], v["line"], f"view {name} sem security_invoker: roda com os privilégios "
                              "do dono e ignora a RLS (advisor security_definer_view)"))
    for bucket, b in state["buckets"].items():
        hints.append(Hint("SQL-13", b["file"], b["line"], f"bucket público '{bucket}' sem allowed_mime_types: "
                          "aceita SVG/HTML com script"))
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


def _secret_matches(path: str, text: str):
    """(rótulo, valor) de cada segredo na linha, com os mesmos filtros da busca na versão atual."""
    if SKIP_SECRET_FILES.search(path) or path.endswith(".md"):
        return
    for label, pattern in SECRET_PATTERNS:
        if label == "chave privada" and PEM_HANDLING.search(text):
            continue
        for m in re.finditer(pattern, text):
            yield label, m.group(0)
    for m in JWT.finditer(text):
        if _jwt_role(m.group(1)) == "service_role":
            yield "JWT com role=service_role", m.group(0)


def history_secret_findings(cfg: Config, repo, sha: str) -> list[dict]:
    """Segredos que já foram removidos do código mas continuam no histórico do git (qualquer clone os tem).

    Os que ainda estão na versão atual já viram pista EF-6; aqui entram só os que sumiram. O valor nunca sai daqui.
    """
    combined = "|".join(p for _, p in SECRET_PATTERNS) + "|" + JWT.pattern
    log = gitrepo._git(cfg, repo, "log", "-p", "--unified=0", "--no-color", "--no-ext-diff", "-G", combined,
                       "--format=@@C %h %cs", sha)
    current = {value for path, _, text in _grep(cfg, repo, sha, combined, ".") for _, value in _secret_matches(path, text)}
    found: dict[tuple, dict] = {}  # (rótulo, arquivo) -> commit mais antigo que adicionou
    commit = date = path = ""
    for line in log.splitlines():
        if line.startswith("@@C "):
            _, commit, date = line.split(" ", 2)
        elif line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else ""
        elif line.startswith("+") and path:
            for label, value in _secret_matches(path, line[1:]):
                if value not in current:  # o log vem do mais novo para o mais antigo: fica o commit de origem
                    found[(label, path)] = {"commit": commit, "date": date}
    out = []
    for (label, path), c in found.items():
        test = bool(TEST_FILE.search(path))
        out.append({
            "regra": "EF-6",
            "severity": "low" if test else "high",
            "cwe": "CWE-798",
            "file": path,
            "line": None,
            "title": f"Possível {label} no histórico do git (commit {c['commit']})",
            "description": (f"Uma possível {label} foi adicionada em {path} no commit {c['commit']} ({c['date']}) e não está "
                            "mais no código atual, mas continua no histórico: qualquer pessoa com acesso ao repositório "
                            "ou a um clone antigo consegue lê-la. O valor foi omitido deste relatório."
                            + (" É um arquivo de teste: provavelmente fictícia, confirmar." if test else "")),
            "suggestion": ("Se a chave era real, troque-a (rotacione) no serviço de origem e revogue a antiga. Apagar do "
                           "histórico não basta: clones e cópias já a têm."),
        })
    return out


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
    mass_assign = r"\.(update|insert|upsert)\(\s*(body|reqBody|requestBody|payload)\s*\)"
    for path, line, _ in _grep(cfg, repo, sha, mass_assign, "supabase/functions", "src"):
        hints.append(Hint("DB-2", path, line,
                          "escrita direta no banco com objeto do corpo/payload sem lista explícita de campos permitidos "
                          "(risco de atribuição em massa de role/organization_id/status)"))
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
