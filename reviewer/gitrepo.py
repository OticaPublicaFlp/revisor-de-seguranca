import base64
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

from .config import Config

# Arquivos que geram ruído e custo sem valor de revisão
IGNORED = re.compile(
    r"(^|/)(package-lock\.json|pnpm-lock\.yaml|yarn\.lock|bun\.lockb?|deno\.lock)$"
    r"|\.(png|jpe?g|gif|webp|ico|svg|woff2?|ttf|pdf|map|min\.js)$"
    r"|(^|/)(dist|build|node_modules)/"
    r"|(^|/)types\.ts$"  # tipos gerados pelo Supabase
)


class GitError(RuntimeError):
    pass


def _env(cfg: Config) -> dict:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if cfg.repo_token:
        # Via env (GIT_CONFIG_*) para o token não aparecer em argv/ps nem em .git/config
        creds = base64.b64encode(f"x-access-token:{cfg.repo_token}".encode()).decode()
        env.update(
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="http.extraHeader",
            GIT_CONFIG_VALUE_0=f"Authorization: Basic {creds}",
        )
    if cfg.repo_ssh_key:
        # Só a chave dedicada (deploy key): ignora agente SSH e ~/.ssh/config do usuário
        env["GIT_SSH_COMMAND"] = (
            f"ssh -F /dev/null -i {shlex.quote(cfg.repo_ssh_key)} -o IdentitiesOnly=yes "
            "-o StrictHostKeyChecking=accept-new -o BatchMode=yes"
        )
    return env


def _git(cfg: Config, workdir: Path | None, *args: str, ok: tuple[int, ...] = (0,)) -> str:
    cmd = ["git", *args]
    result = subprocess.run(
        cmd, cwd=workdir, env=_env(cfg), capture_output=True, text=True,
        errors="replace", timeout=120,
    )
    if result.returncode not in ok:
        # stderr pode ecoar a URL; o token não está nela, então é seguro logar
        raise GitError(f"git {args[0]} falhou: {result.stderr.strip()}")
    return result.stdout


NETWORK_ERROR = re.compile(r"unable to access|Could not resolve|Failed to connect|timed out|Connection (reset|refused)"
                           r"|Temporary failure|early EOF|RPC failed", re.I)
RETRY_WAITS = (5, 20)  # segundos entre as 3 tentativas


def _with_retry(fn):
    """Repete operações de rede do git: o WSL perde a conexão ao hibernar ou trocar de rede."""
    for wait in (*RETRY_WAITS, None):
        try:
            return fn()
        except GitError as exc:
            if wait is None or not NETWORK_ERROR.search(str(exc)):
                raise
            time.sleep(wait)


def sync(cfg: Config) -> tuple[Path, str]:
    """Clona (ou atualiza) a branch remota e retorna (diretório, SHA do HEAD remoto)."""
    repo = cfg.data_dir / "repo"
    if not (repo / ".git").exists():
        _with_retry(lambda: _git(cfg, None, "clone", "--no-tags", "--branch", cfg.repo_branch,
                                 cfg.repo_url, str(repo)))
    else:
        _with_retry(lambda: _git(cfg, repo, "fetch", "--no-tags", "origin", cfg.repo_branch))
    head = _git(cfg, repo, "rev-parse", f"origin/{cfg.repo_branch}").strip()
    return repo, head


def resolve_base(cfg: Config, repo: Path, last_sha: str | None, head: str) -> str:
    if last_sha:
        # Se o histórico foi reescrito (force-push) o SHA antigo pode não existir mais
        try:
            _git(cfg, repo, "cat-file", "-e", f"{last_sha}^{{commit}}")
            return last_sha
        except GitError:
            pass
    count = int(_git(cfg, repo, "rev-list", "--count", head).strip())
    back = min(cfg.initial_lookback, max(count - 1, 0))
    return _git(cfg, repo, "rev-parse", f"{head}~{back}").strip()


def diff_by_file(cfg: Config, repo: Path, base: str, head: str) -> list[tuple[str, str]]:
    """Retorna [(arquivo, diff)] já filtrado, sem arquivos ignorados."""
    raw = _git(cfg, repo, "diff", "--unified=4", "--no-color", "--no-ext-diff", base, head)
    parts = re.split(r"(?m)^(?=diff --git )", raw)
    out = []
    for part in parts:
        m = re.match(r"diff --git a/(.+?) b/(.+)\n", part)
        if not m:
            continue
        path = m.group(2)
        if IGNORED.search(path) or "Binary files" in part:
            continue
        out.append((path, part))
    return out


def chunk_diffs(files: list[tuple[str, str]], limit: int) -> list[str]:
    """Agrupa diffs por arquivo em blocos de no máximo `limit` caracteres."""
    chunks, current, size = [], [], 0
    for _, text in files:
        if len(text) > limit:
            text = text[:limit] + "\n[... diff truncado ...]\n"
        if size + len(text) > limit and current:
            chunks.append("".join(current))
            current, size = [], 0
        current.append(text)
        size += len(text)
    if current:
        chunks.append("".join(current))
    return chunks
