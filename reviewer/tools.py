"""Ferramentas SOMENTE LEITURA que o revisor usa para consultar o código da branch revisada.

Tudo passa por objetos do git (`git show`, `git grep`, `git ls-tree`) no SHA fixo da revisão:
não há acesso ao working tree, a symlinks nem a nada fora do repositório.
"""
import re

from . import gitrepo
from .config import Config

MAX_FILE_LINES = 400
MAX_RESULT_CHARS = 12000
MAX_GREP_MATCHES = 25

# Caminhos que nunca vão para o modelo, mesmo se alguém os versionar por engano
DENY = re.compile(
    r"(^|/)\.env(\..*)?$|(^|/)\.git(/|$)|(^|/)node_modules(/|$)"
    r"|\.(pem|key|p12|pfx|keystore)$|(^|/)(id_rsa|id_ed25519)(\.pub)?$|(^|/)\.npmrc$"
)

TOOLS = [
    {
        "name": "read_file",
        "description": "Lê um arquivo da branch revisada (versão atual, não o diff). "
                       "Retorna até 400 linhas numeradas; use start_line para continuar.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Caminho relativo à raiz do repositório"},
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1, "description": "Última linha (opcional; máx. 400 linhas por leitura)"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "search_repo",
        "description": "Busca um texto no código da branch revisada (git grep). Retorna até 25 ocorrências "
                       "no formato arquivo:linha:trecho. Use para achar quem importa um _shared, onde a "
                       "autenticação acontece ou se um GRANT existe.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "regex": {"type": "boolean", "description": "true = expressão regular estendida; padrão: texto literal"},
                "path": {"type": "string", "description": "Pasta ou arquivo para restringir a busca (opcional)"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "list_dir",
        "description": "Lista os nomes dentro de uma pasta da branch revisada (vazio = raiz).",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
        },
    },
    {
        "name": "inspect_symbol",
        "description": "Raio-X rápido de uma tabela SQL, função/RPC ou módulo compartilhado na branch revisada: "
                       "mostra onde é definido/alterado nas migrations/config e onde é chamado nas Edge Functions e no front-end. "
                       "Economiza várias chamadas de search_repo.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Nome da tabela, função SQL, Edge Function ou módulo (ex.: otica_patients, search_patients, role-guard)"},
            },
            "required": ["name"],
        },
    },
]


class ToolError(Exception):
    pass


def _clean_path(raw: str | None) -> str:
    path = re.sub(r"^(\./)+", "", (raw or "").strip()).rstrip("/")
    if "\x00" in path or path.startswith(("/", "-", "~")) or ".." in path.split("/"):
        raise ToolError("caminho inválido: use um caminho relativo dentro do repositório")
    if DENY.search(path):
        raise ToolError("caminho bloqueado por política (segredos/dependências)")
    return path


def _cap(text: str) -> str:
    if len(text) > MAX_RESULT_CHARS:
        return text[:MAX_RESULT_CHARS] + "\n[... resultado truncado ...]"
    return text


def read_file(cfg: Config, repo, head: str, path: str, start_line: int = 1, end_line: int | None = None) -> str:
    path = _clean_path(path)
    if not path:
        raise ToolError("informe o caminho do arquivo")
    if gitrepo._git(cfg, repo, "cat-file", "-t", f"{head}:{path}", ok=(0, 128)).strip() != "blob":
        raise ToolError("arquivo não encontrado (use list_dir/search_repo para localizar)")
    content = gitrepo._git(cfg, repo, "show", f"{head}:{path}")
    if "\x00" in content:
        return f"<arquivo caminho={path!r}>[arquivo binário]</arquivo>"
    lines = content.splitlines()
    start = max(start_line, 1)
    stop = min(end_line, start - 1 + MAX_FILE_LINES) if end_line and end_line >= start else start - 1 + MAX_FILE_LINES
    chunk = lines[start - 1: stop]
    body = "\n".join(f"{start + i:>5}  {line}" for i, line in enumerate(chunk))
    end = start + len(chunk) - 1
    tail = f"\n[continua: {len(lines)} linhas no total; use start_line={end + 1}]" if end < len(lines) else ""
    return _cap(f"<arquivo caminho={path!r} linhas='{start}-{end}'>\n{body}{tail}\n</arquivo>")


def search_repo(cfg: Config, repo, head: str, pattern: str, regex: bool = False, path: str | None = None) -> str:
    if not pattern.strip():
        raise ToolError("informe o texto a buscar")
    scope = _clean_path(path)
    args = ["grep", "-n", "-I", "--no-color", "-m", "5", "-E" if regex else "-F", "-e", pattern, head]
    if scope:
        args += ["--", scope]
    out = gitrepo._git(cfg, repo, *args, ok=(0, 1))  # 1 = nenhuma ocorrência
    prefix = f"{head}:"
    hits = []
    for line in out.splitlines():
        line = line[len(prefix):] if line.startswith(prefix) else line
        if DENY.search(line.split(":", 1)[0]):
            continue
        hits.append(line[:240])
        if len(hits) >= MAX_GREP_MATCHES:
            break
    return _cap("\n".join(hits)) if hits else "nenhuma ocorrência"


def list_dir(cfg: Config, repo, head: str, path: str | None = None) -> str:
    target = _clean_path(path)
    spec = f"{head}:{target}" if target else head
    out = gitrepo._git(cfg, repo, "ls-tree", "--name-only", spec, ok=(0, 128))
    names = [n for n in out.splitlines() if not DENY.search(n)]
    return _cap("\n".join(names)) if names else "pasta vazia ou inexistente"


def inspect_symbol(cfg: Config, repo, head: str, name: str) -> str:
    sym = (name or "").strip()
    if not sym or "\x00" in sym or len(sym) > 120:
        raise ToolError("informe o nome da tabela, função ou módulo a inspecionar")

    def _hits(scope: str, limit: int) -> list[str]:
        out = gitrepo._git(cfg, repo, "grep", "-n", "-I", "--no-color", "-m", "5", "-F", "-e", sym,
                           head, "--", scope, ok=(0, 1))
        prefix = f"{head}:"
        rows = []
        for line in out.splitlines():
            line = line[len(prefix):] if line.startswith(prefix) else line
            if DENY.search(line.split(":", 1)[0]):
                continue
            rows.append(line[:240])
            if len(rows) >= limit:
                break
        return rows

    sql_hits = _hits("supabase/migrations", 20) + _hits("supabase/config.toml", 5)
    code_hits = _hits("supabase/functions", 20) + _hits("src", 20)
    sections = [
        "## Migrations / Config\n" + ("\n".join(sql_hits) if sql_hits else "nenhuma ocorrência"),
        "## Código (Edge Functions / Front-end)\n" + ("\n".join(code_hits) if code_hits else "nenhuma ocorrência"),
    ]
    return _cap("\n\n".join(sections))


def execute(cfg: Config, repo, head: str, name: str, args: dict) -> str:
    """Executa uma ferramenta e devolve texto. Erros viram mensagem para o modelo, nunca exceção."""
    try:
        if name == "read_file":
            return read_file(cfg, repo, head, args.get("path", ""), int(args.get("start_line") or 1),
                             int(args["end_line"]) if args.get("end_line") else None)
        if name == "search_repo":
            return search_repo(cfg, repo, head, args.get("pattern", ""), bool(args.get("regex")), args.get("path"))
        if name == "list_dir":
            return list_dir(cfg, repo, head, args.get("path"))
        if name == "inspect_symbol":
            return inspect_symbol(cfg, repo, head, args.get("name", ""))
        return f"ferramenta desconhecida: {name}"
    except (ToolError, gitrepo.GitError) as exc:
        return f"erro: {exc}"
