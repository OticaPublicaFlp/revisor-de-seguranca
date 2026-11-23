"""Assistente de configuração (`check configurar`) e diagnóstico (`check verificar`).

Interface de menu navegável por setas (↑/↓ + Enter, Esc para voltar), sem precisar decorar nomes
de variáveis no `.env`. Valida cada credencial na hora (sem gastar créditos de IA) e só grava o
`.env` ao salvar, com backup do anterior e permissão 600.
"""
import builtins
import getpass
import os
import re
import select
import shutil
import stat
import subprocess
import sys
import termios
import tty
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import httpx

from . import gitrepo, profile as profiles, ui
from .config import ROOT

ENV = ROOT / ".env"
EXAMPLE = ROOT / ".env.example"

MODELS = [
    ("claude-opus-5-5", "Claude Opus 5.5 — o mais preciso (recomendado para segurança)"),
    ("claude-sonnet-5-5", "Claude Sonnet 5.5 — metade do custo, um pouco menos minucioso"),
]
# Custo aproximado por unidade revisada (US$), medido nos nossos testes. O valor real aparece no fim de cada revisão.
COST_PER_UNIT = {("claude-opus-5-5", True): 0.80, ("claude-opus-5-5", False): 0.45,
                 ("claude-sonnet-5-5", True): 0.40, ("claude-sonnet-5-5", False): 0.22}
SCHEDULES = [
    ("0 8 * * *", "Todo dia às 8h"),
    ("0 8 * * 1-5", "De segunda a sexta às 8h"),
    ("0 8 * * 1", "Toda segunda-feira às 8h"),
    ("0 7 * * *", "Todo dia às 7h"),
    ("0 18 * * *", "Todo dia às 18h"),
    ("custom", "✎ Outra expressão cron..."),
]
EFFORTS = [
    ("low", "Baixa (low) — mais rápido e barato"),
    ("medium", "Média (medium) — equilíbrio entre custo e profundidade (recomendado)"),
    ("high", "Alta (high) — analisa com mais cuidado"),
    ("xhigh", "Muito alta (xhigh) — raciocínio longo para código complexo"),
    ("max", "Máxima (max) — profundidade total (mais lento e caro)"),
]
REVIEW_MODES = [
    ("code", "Por partes do código (code) — revisa a base aos poucos e lembra o que já viu (recomendado)"),
    ("diff", "Só o que mudou (diff) — revisa apenas os commits novos desde a última execução"),
]
DUAL_MODES = [
    ("critical", "Nas partes críticas (critical) — funções, banco/migrations e temas (recomendado)"),
    ("all", "Em tudo (all) — todas as partes passam também pelo atacante (mais caro)"),
    ("off", "Desligada (off) — apenas a revisão do checklist"),
]
TIMEZONES = [
    ("America/Sao_Paulo", "America/Sao_Paulo (Brasília)"),
    ("America/Manaus", "America/Manaus"),
    ("America/Fortaleza", "America/Fortaleza"),
    ("UTC", "UTC"),
    ("Europe/Lisbon", "Europe/Lisbon"),
]

DEFAULTS = {
    "PROFILE": "exemplo",
    "REPO_URL": "",
    "REPO_BRANCH": "main",
    "REPO_TOKEN": "",
    "REPO_SSH_KEY": "",
    "REVIEW_MODEL": "claude-opus-5-5",
    "ANTHROPIC_API_KEY": "",
    "EFFORT": "medium",
    "REVIEW_MODE": "code",
    "DUAL_REVIEW": "critical",
    "VERIFY": "true",
    "MAX_UNITS": "5",
    "MAX_TURNS": "14",
    "RECHECK_DAYS": "30",
    "DEPS_CHECK": "true",
    "CHUNK_CHARS": "60000",
    "MAX_OUTPUT_TOKENS": "32000",
    "MAX_CHUNKS": "6",
    "INITIAL_LOOKBACK_COMMITS": "20",
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_CHAT_ID": "",
    "TELEGRAM_APPROVER_IDS": "",
    "SLACK_BOT_TOKEN": "",
    "SLACK_CHANNEL_ID": "",
    "SLACK_INCLUDE_DETAILS": "false",
    "CRON_EXPR": "0 8 * * *",
    "TZ": "America/Sao_Paulo",
    "DATA_DIR": "./data",
}

_BACK = "__back__"
_CUSTOM = "__custom__"
_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


# ---------------------------------------------------------------- .env

def read_env(path: Path | None = None) -> dict[str, str]:
    path = path or ENV  # lido na hora da chamada (um valor padrão fixo ignoraria a troca de ENV nos testes)
    values = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*([A-Z][A-Z0-9_]*)=(.*)$", line)
            if m:
                values[m.group(1)] = m.group(2).strip()
    return values


def write_env(values: dict[str, str], path: Path | None = None, template: Path | None = None) -> Path | None:
    """Grava seguindo a ordem e os comentários do .env.example. Faz backup do .env anterior. Permissão 600."""
    path, template = path or ENV, template or EXAMPLE
    backup = None
    if path.is_file():
        backup = path.with_name(f".env.bak-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(path, backup)
        backup.chmod(stat.S_IRUSR | stat.S_IWUSR)
    lines, seen = [], set()
    for line in template.read_text(encoding="utf-8").splitlines():
        m = re.match(r"([A-Z][A-Z0-9_]*)=", line)
        if m and m.group(1) in values:
            lines.append(f"{m.group(1)}={values[m.group(1)]}")
            seen.add(m.group(1))
        else:
            lines.append(line)
    extra = [f"{k}={v}" for k, v in values.items() if k not in seen]
    if extra:
        lines += ["", "# --- Outras opções", *extra]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return backup


def mask(value: str) -> str:
    if not value:
        return "(vazio)"
    return "••••" + value[-4:] if len(value) > 10 else "••••"


# ---------------------------------------------------------------- terminal e seleção por setas

def _c(code: str, text: str) -> str:
    return ui._c(code, text)


def say(text: str = "") -> None:
    print(text, flush=True)


def ok(text: str) -> None:
    say(_c("32", f"  ✓ {text}"))


def fail(text: str) -> None:
    say(_c("31", f"  ✗ {text}"))


def _use_raw_menu() -> bool:
    """True em terminal interativo real; False quando stdin/stdout não é TTY ou `input` está mockado."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return False
    if hasattr(builtins.input, "assert_called"):
        return False
    try:
        termios.tcgetattr(sys.stdin.fileno())
        return True
    except Exception:
        return False


def _read_key() -> str:
    """Lê uma tecla diretamente do file descriptor (sem buffer) em modo cbreak."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd, termios.TCSANOW)
        data = os.read(fd, 1)
        if data == b"\x1b" and select.select([fd], [], [], 0.05)[0]:
            data += os.read(fd, 16)
        if data in (b"\r", b"\n"):
            return "ENTER"
        if data == b"\x03":
            raise KeyboardInterrupt
        if data in (b"\x1b[A", b"\x1bOA", b"k", b"K"):
            return "UP"
        if data in (b"\x1b[B", b"\x1bOB", b"j", b"J"):
            return "DOWN"
        if data in (b"\x1b", b"q", b"Q"):
            return "ESC"
        if len(data) == 1 and b"1" <= data <= b"9":
            return data.decode("ascii")
        return "OTHER"
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, old)


def _fit_line(text: str, cols: int) -> str:
    """Trunca texto sem códigos ANSI para caber em uma única linha do terminal."""
    plain = _ANSI_RE.sub("", text)
    if len(plain) <= cols:
        return text
    # Se há códigos ANSI, trunca o texto simples para não quebrar sequências no meio.
    return plain[: max(10, cols - 1)] + "…"


def _choose_raw(label: str, options: list[tuple[str, str]], default: str | None, allow_esc: bool) -> str:
    idx = next((i for i, (v, _) in enumerate(options) if v == default), 0)
    cols = max(40, shutil.get_terminal_size((100, 24)).columns - 1)
    hint = "↑/↓ navegar · Enter selecionar" + (" · Esc voltar" if allow_esc else "")

    def render(first: bool = False) -> None:
        if not first:
            sys.stdout.write(f"\033[{len(options)}A")
        for i, (value, text) in enumerate(options):
            active = i == idx
            cur_mark = _c("32", " ●") if (default is not None and value == default) else ""
            if active:
                line = f"  {_c('1;36', '❯')} {_c('1', text)}{cur_mark}"
            else:
                line = f"    {_c('90' if value in (_BACK, 'quit') else '0', text)}{cur_mark}"
            sys.stdout.write("\r\033[K" + _fit_line(line, cols) + "\n")
        sys.stdout.flush()

    say()
    say(f"  {_c('1;36', label)}  {_c('90', f'({hint})')}")
    sys.stdout.write("\033[?25l")
    sys.stdout.flush()
    try:
        render(first=True)
        while True:
            key = _read_key()
            if key == "UP":
                idx = (idx - 1) % len(options)
                render()
            elif key == "DOWN":
                idx = (idx + 1) % len(options)
                render()
            elif key == "ENTER":
                picked_val, picked_txt = options[idx]
                sys.stdout.write(f"\033[{len(options) + 1}A\r\033[J")
                if picked_val != _BACK:
                    say(f"  {_c('90', label + ':')} {_c('36', _ANSI_RE.sub('', picked_txt))}")
                sys.stdout.flush()
                return picked_val
            elif key == "ESC" and allow_esc:
                sys.stdout.write(f"\033[{len(options) + 1}A\r\033[J")
                sys.stdout.flush()
                return _BACK
            elif key.isdigit() and 1 <= int(key) <= len(options):
                picked_val, picked_txt = options[int(key) - 1]
                sys.stdout.write(f"\033[{len(options) + 1}A\r\033[J")
                if picked_val != _BACK:
                    say(f"  {_c('90', label + ':')} {_c('36', _ANSI_RE.sub('', picked_txt))}")
                sys.stdout.flush()
                return picked_val
    finally:
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()


def choose(label: str, options: list[tuple[str, str]], default: str | None = None,
           allow_esc: bool = False) -> str:
    if _use_raw_menu():
        return _choose_raw(label, options, default, allow_esc)

    say(f"  {label}")
    for i, (value, text) in enumerate(options, 1):
        mark = _c("32", " (atual)") if (default is not None and value == default) else ""
        say(f"    {i}) {text}{mark}")
    default_idx = next((str(i) for i, (v, _) in enumerate(options, 1) if v == default), "1")
    option_values = {v for v, _ in options}
    while True:
        pick = input(f"  Escolha [{default_idx}]: ").strip() or default_idx
        if allow_esc and pick.lower() in ("esc", "q", "voltar", "back"):
            return _BACK
        if pick in option_values:
            return pick
        if pick.isdigit() and 1 <= int(pick) <= len(options):
            return options[int(pick) - 1][0]
        say(_c("33", f"  ⚠ digite um número de 1 a {len(options)}"))


def ask(label: str, default: str = "", validate=None) -> str:
    while True:
        hint = f" [{default}]" if default else ""
        value = input(f"  {label}{hint}: ").strip() or default
        if not value:
            say(_c("33", "  ⚠ obrigatório"))
            continue
        problem = validate(value) if validate else None
        if not problem:
            return value
        say(_c("33", f"  ⚠ {problem}"))


def ask_secret(label: str, current: str = "") -> str:
    keep = f" (Enter mantém {mask(current)})" if current else ""
    while True:
        value = getpass.getpass(f"  {label}{keep} — o que você digitar não aparece: ").strip()
        if value or current:
            return value or current
        say(_c("33", "  ⚠ obrigatório"))


def yes(label: str, default: bool = True) -> bool:
    if _use_raw_menu():
        opts = [("s", "Sim"), ("n", "Não")]
        return choose(label, opts, "s" if default else "n", allow_esc=False) == "s"
    hint = "S/n" if default else "s/N"
    answer = input(f"  {label} [{hint}]: ").strip().lower()
    return default if not answer else answer in ("s", "sim", "y", "yes")


def choose_number(label: str, current: str, presets: list[tuple[str, str]],
                  min_v: int, max_v: int, unit_label: str = "") -> str:
    """Seleciona um número por presets (com setas) ou permite digitar um valor personalizado."""
    opts = list(presets)
    if current and all(val != current for val, _ in opts):
        suffix = f" {unit_label}" if unit_label else ""
        opts.insert(0, (current, f"{current}{suffix} (valor atual)"))
    opts.append((_CUSTOM, "✎ Digitar outro valor..."))
    opts.append((_BACK, "← Voltar"))

    pick = choose(label, opts, current, allow_esc=True)
    if pick == _BACK:
        return current
    if pick == _CUSTOM:
        return ask(
            f"{label} ({min_v} a {max_v})",
            current,
            validate=lambda x: None if x.isdigit() and min_v <= int(x) <= max_v else f"um número de {min_v} a {max_v}",
        )
    return pick


def _toggle(v: dict[str, str], key: str) -> None:
    v[key] = "false" if v.get(key, "true").lower() == "true" else "true"


def _on_off(val: str) -> str:
    return _c("32", "Ligado ✓") if val.lower() == "true" else _c("90", "Desligado ✗")


# ---------------------------------------------------------------- validações (sem custo de IA)

def check_repo(url: str, branch: str, token: str = "", ssh_key: str = "") -> tuple[bool, str]:
    cfg = SimpleNamespace(repo_token=token, repo_ssh_key=ssh_key)
    try:
        r = subprocess.run(["git", "ls-remote", "--heads", url, branch], env=gitrepo._env(cfg),
                           capture_output=True, text=True, timeout=45)
    except subprocess.TimeoutExpired:
        return False, "o GitHub não respondeu em 45 s (rede?)"
    if r.returncode != 0:
        err = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "erro desconhecido"
        return False, f"sem acesso ao repositório ({err[:160]})"
    if not r.stdout.strip():
        return False, f"o repositório respondeu, mas a branch '{branch}' não existe"
    return True, f"repositório acessível, branch '{branch}' encontrada"


def check_anthropic(key: str, model: str) -> tuple[bool, str]:
    import anthropic
    try:
        anthropic.Anthropic(api_key=key, max_retries=1, timeout=30).models.retrieve(model)
        return True, f"chave aceita e modelo {model} disponível (consulta gratuita)"
    except anthropic.AuthenticationError:
        return False, "a Anthropic recusou a chave (confira se copiou inteira)"
    except anthropic.NotFoundError:
        return False, f"a chave funciona, mas o modelo {model} não está disponível para ela"
    except anthropic.APIError as exc:
        return False, f"não consegui validar agora ({type(exc).__name__}); dá para seguir e testar depois"


def telegram_call(token: str, method: str, **payload) -> dict:
    data = httpx.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=30).json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "erro do Telegram"))
    return data["result"]


def telegram_scan(token: str) -> tuple[dict[int, str], dict[int, str]]:
    """Grupos/conversas e pessoas que o bot já viu (não consome as mensagens)."""
    chats, people = {}, {}
    updates = telegram_call(token, "getUpdates", timeout=0, allowed_updates=["message", "my_chat_member"])
    for upd in updates:
        obj = upd.get("message") or upd.get("my_chat_member") or {}
        chat = obj.get("chat")
        if chat:
            chats[chat["id"]] = chat.get("title") or chat.get("first_name") or str(chat["id"])
        sender = obj.get("from")
        if sender and not sender.get("is_bot"):
            people[sender["id"]] = " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")]))
    return chats, people


def check_slack(token: str) -> tuple[bool, str]:
    try:
        data = httpx.post("https://slack.com/api/auth.test", headers={"Authorization": f"Bearer {token}"},
                          timeout=30).json()
    except httpx.HTTPError as exc:
        return False, f"sem conexão com o Slack ({type(exc).__name__})"
    if data.get("ok"):
        return True, f"token aceito (workspace {data.get('team', '?')})"
    return False, f"o Slack recusou o token ({data.get('error')})"


# ---------------------------------------------------------------- helpers de resumo

def _short_model(model: str) -> str:
    return "Opus 5.5" if "opus" in model else "Sonnet 5.5" if "sonnet" in model else model


def _short_repo(v: dict[str, str]) -> str:
    if not v.get("REPO_URL"):
        return "⚠ não configurado"
    tail = v["REPO_URL"].rstrip("/").split("/")[-1].removesuffix(".git")
    return f"{tail} ({v['REPO_BRANCH']})"


def _short_telegram(v: dict[str, str]) -> str:
    if not v.get("TELEGRAM_BOT_TOKEN"):
        return "desativado (só simulação)"
    return f"chat {v['TELEGRAM_CHAT_ID']}" if v.get("TELEGRAM_CHAT_ID") else "bot sem chat"


def _short_slack(v: dict[str, str]) -> str:
    if not v.get("SLACK_BOT_TOKEN"):
        return "desativado"
    return f"canal {v['SLACK_CHANNEL_ID']}" if v.get("SLACK_CHANNEL_ID") else "sem canal"


def _short_cron(expr: str) -> str:
    labels = {val: desc for val, desc in SCHEDULES if val != "custom"}
    return labels.get(expr, expr)


# ---------------------------------------------------------------- submenus

def _new_profile() -> str:
    while True:
        slug = ask("Nome curto do perfil (letras minúsculas, números e hífen)", validate=lambda v: None if re.fullmatch(
            r"[a-z0-9][a-z0-9-]{1,40}", v) else "use só letras minúsculas, números e hífen")
        if slug in profiles.available():
            say(_c("33", "  ⚠ já existe um perfil com esse nome"))
            continue
        project = ask("Nome do projeto (aparece nos relatórios)")
        dest = profiles.PROFILES_DIR / slug
        shutil.copytree(profiles.PROFILES_DIR / "exemplo", dest)
        toml = dest / "perfil.toml"
        toml.write_text(re.sub(r'(?m)^nome = ".*"$', f'nome = "{project}"', toml.read_text(encoding="utf-8")),
                        encoding="utf-8")
        ok(f"perfil criado em profiles/{slug}/ (copiado do exemplo; fica só no seu computador)")
        say(_c("33", f"  → Depois, edite profiles/{slug}/contexto.md (o que o sistema faz) e regras.md (o que revisar)."))
        return slug


def _menu_projeto(v: dict[str, str]) -> None:
    options = [(p, f"{p} — {profiles.load(p).project}") for p in profiles.available()]
    options.append(("__novo__", "+ Criar um perfil novo para o meu projeto"))
    options.append((_BACK, "← Voltar"))
    pick = choose("Projeto e Perfil", options, v["PROFILE"], allow_esc=True)
    if pick == _BACK:
        return
    v["PROFILE"] = _new_profile() if pick == "__novo__" else pick


def _setup_repo_auth(v: dict[str, str]) -> None:
    cur_method = "token" if v.get("REPO_TOKEN") else "ssh" if v.get("REPO_SSH_KEY") else "helper"
    method = choose("Como o revisor acessa o repositório?", [
        ("helper", "Credencial do git deste computador (mais simples)"),
        ("token", "Token do GitHub só de leitura (recomendado: Contents: Read)"),
        ("ssh", "Chave SSH só de leitura (deploy key)"),
        ("public", "Repositório público (sem credencial)"),
        (_BACK, "← Voltar"),
    ], cur_method, allow_esc=True)
    if method == _BACK:
        return
    v["REPO_TOKEN"], v["REPO_SSH_KEY"] = "", ""
    if method == "token":
        v["REPO_TOKEN"] = ask_secret("Token do GitHub", v.get("REPO_TOKEN", ""))
    elif method == "ssh":
        v["REPO_SSH_KEY"] = ask(
            "Caminho da chave privada",
            v.get("REPO_SSH_KEY") or str(Path.home() / ".ssh/revisor_deploy"),
            validate=lambda p: None if Path(p).expanduser().is_file() else "arquivo não encontrado",
        )
        if v.get("REPO_URL") and not v["REPO_URL"].startswith("git@"):
            say(_c("33", "  ⚠ com chave SSH, a URL precisa ser no formato git@github.com:empresa/projeto.git"))


def _menu_repo(v: dict[str, str]) -> None:
    last = "url"
    while True:
        auth_lbl = ("Token GitHub" if v.get("REPO_TOKEN") else
                    f"SSH ({Path(v['REPO_SSH_KEY']).name})" if v.get("REPO_SSH_KEY") else "Git local / público")
        pick = choose("Repositório Git", [
            ("url", f"URL do repositório          [{v.get('REPO_URL') or '⚠ vazio'}]"),
            ("branch", f"Branch a revisar            [{v['REPO_BRANCH']}]"),
            ("auth", f"Credencial de acesso        [{auth_lbl}]"),
            ("test", "Testar acesso ao repositório agora"),
            (_BACK, "← Voltar ao menu principal"),
        ], last, allow_esc=True)
        if pick in (_BACK,):
            return
        last = pick
        if pick == "url":
            v["REPO_URL"] = ask("URL do repositório (ex.: https://github.com/empresa/projeto.git)",
                                v.get("REPO_URL", ""),
                                validate=lambda u: None if re.match(r"(https://|git@)", u) else "comece com https:// ou git@")
        elif pick == "branch":
            b = choose("Branch a revisar", [
                ("main", "main"),
                ("master", "master"),
                ("develop", "develop"),
                (_CUSTOM, "✎ Digitar outra branch..."),
                (_BACK, "← Voltar"),
            ], v["REPO_BRANCH"], allow_esc=True)
            if b == _CUSTOM:
                v["REPO_BRANCH"] = ask("Nome da branch", v["REPO_BRANCH"])
            elif b != _BACK:
                v["REPO_BRANCH"] = b
        elif pick == "auth":
            _setup_repo_auth(v)
        elif pick == "test":
            if not v.get("REPO_URL"):
                fail("Informe a URL do repositório primeiro.")
                continue
            say(_c("90", "  testando o acesso..."))
            good, msg = check_repo(v["REPO_URL"], v["REPO_BRANCH"], v.get("REPO_TOKEN", ""), v.get("REPO_SSH_KEY", ""))
            (ok if good else fail)(msg)


def _menu_ia(v: dict[str, str]) -> None:
    last = "model"
    while True:
        pick = choose("Inteligência Artificial (Anthropic)", [
            ("model", f"Modelo                      [{_short_model(v['REVIEW_MODEL'])}]"),
            ("effort", f"Profundidade / Esforço      [{v['EFFORT']}]"),
            ("key", f"Chave da API Anthropic      [{mask(v.get('ANTHROPIC_API_KEY', ''))}]"),
            ("test", "Testar chave e modelo agora (gratuito)"),
            (_BACK, "← Voltar ao menu principal"),
        ], last, allow_esc=True)
        if pick == _BACK:
            return
        last = pick
        if pick == "model":
            m = choose("Modelo", [*MODELS, (_BACK, "← Voltar")], v["REVIEW_MODEL"], allow_esc=True)
            if m != _BACK:
                v["REVIEW_MODEL"] = m
        elif pick == "effort":
            e = choose("Profundidade da análise (esforço)", [*EFFORTS, (_BACK, "← Voltar")], v["EFFORT"], allow_esc=True)
            if e != _BACK:
                v["EFFORT"] = e
        elif pick == "key":
            v["ANTHROPIC_API_KEY"] = ask_secret("Chave da Anthropic (começa com sk-ant-)", v.get("ANTHROPIC_API_KEY", ""))
            say(_c("90", "  testando a chave..."))
            good, msg = check_anthropic(v["ANTHROPIC_API_KEY"], v["REVIEW_MODEL"])
            (ok if good else fail)(msg)
        elif pick == "test":
            if not v.get("ANTHROPIC_API_KEY"):
                fail("Informe a chave da Anthropic primeiro.")
                continue
            say(_c("90", "  testando a chave..."))
            good, msg = check_anthropic(v["ANTHROPIC_API_KEY"], v["REVIEW_MODEL"])
            (ok if good else fail)(msg)


def _menu_ritmo(v: dict[str, str]) -> None:
    last = "units"
    while True:
        dual = v["DUAL_REVIEW"] != "off"
        unit_cost = COST_PER_UNIT.get((v["REVIEW_MODEL"], dual), 0.8)
        est = int(v["MAX_UNITS"]) * unit_cost if v["MAX_UNITS"].isdigit() else 0.0
        est_txt = f"≈ US$ {est:.2f}/execução".replace(".", ",")
        pick = choose(f"Ritmo, Unidades e Voltas  ({est_txt})", [
            ("units", f"Partes por execução         [{v['MAX_UNITS']} parte(s)]"),
            ("turns", f"Limite de voltas da IA      [{v['MAX_TURNS']} voltas por etapa]"),
            ("mode", f"Modo de revisão             [{v['REVIEW_MODE']}]"),
            ("dual", f"Segunda revisão (atacante)  [{v['DUAL_REVIEW']}]"),
            ("verify", f"Verificação anti-falso pos. [{_on_off(v['VERIFY'])}]  (Enter alterna)"),
            ("recheck", f"Revisar de novo sem mudança [{v['RECHECK_DAYS']} dias]"),
            ("deps", f"Checar libs no osv.dev      [{_on_off(v['DEPS_CHECK'])}]  (Enter alterna)"),
            (_BACK, "← Voltar ao menu principal"),
        ], last, allow_esc=True)
        if pick == _BACK:
            return
        last = pick
        if pick == "units":
            presets = [
                ("1", f"1 parte   (≈ US$ {1 * unit_cost:.2f})".replace(".", ",")),
                ("2", f"2 partes  (≈ US$ {2 * unit_cost:.2f})".replace(".", ",")),
                ("5", f"5 partes — padrão (≈ US$ {5 * unit_cost:.2f})".replace(".", ",")),
                ("10", f"10 partes (≈ US$ {10 * unit_cost:.2f})".replace(".", ",")),
                ("25", f"25 partes (≈ US$ {25 * unit_cost:.2f})".replace(".", ",")),
                ("50", f"50 partes (≈ US$ {50 * unit_cost:.2f})".replace(".", ",")),
                ("150", "150 partes — base inteira de uma vez"),
            ]
            v["MAX_UNITS"] = choose_number("Quantas partes revisar por execução?", v["MAX_UNITS"],
                                           presets, 1, 500, "partes")
        elif pick == "turns":
            presets = [
                ("6", "6 voltas — rápido (pouca consulta a outros arquivos)"),
                ("10", "10 voltas — moderado"),
                ("14", "14 voltas — padrão (recomendado)"),
                ("20", "20 voltas — explora mais helpers e migrations"),
                ("30", "30 voltas — investigação profunda"),
                ("50", "50 voltas — máximo estendido"),
            ]
            v["MAX_TURNS"] = choose_number(
                "Limite de 'voltas' (consultas a outros arquivos por etapa)",
                v["MAX_TURNS"], presets, 1, 60, "voltas",
            )
        elif pick == "mode":
            m = choose("Modo de revisão", [*REVIEW_MODES, (_BACK, "← Voltar")], v["REVIEW_MODE"], allow_esc=True)
            if m != _BACK:
                v["REVIEW_MODE"] = m
        elif pick == "dual":
            d = choose("Segunda revisão com olhar de atacante", [*DUAL_MODES, (_BACK, "← Voltar")],
                       v["DUAL_REVIEW"], allow_esc=True)
            if d != _BACK:
                v["DUAL_REVIEW"] = d
        elif pick == "verify":
            _toggle(v, "VERIFY")
        elif pick == "recheck":
            presets = [
                ("7", "7 dias — semanal"),
                ("15", "15 dias — quinzenal"),
                ("30", "30 dias — padrão (mensal)"),
                ("60", "60 dias"),
                ("90", "90 dias — trimestral"),
            ]
            v["RECHECK_DAYS"] = choose_number(
                "Depois de quantos dias revisar de novo uma parte sem alteração?",
                v["RECHECK_DAYS"], presets, 1, 3650, "dias",
            )
        elif pick == "deps":
            _toggle(v, "DEPS_CHECK")


def _setup_telegram_guided(v: dict[str, str]) -> None:
    while True:
        token = ask_secret("Token do bot (@BotFather)", v.get("TELEGRAM_BOT_TOKEN", ""))
        try:
            bot = telegram_call(token, "getMe")["username"]
            v["TELEGRAM_BOT_TOKEN"] = token
            ok(f"bot @{bot} encontrado")
            break
        except Exception as exc:
            fail(f"o Telegram recusou o token ({str(exc)[:80]})")
            if not yes("Tentar outro token?", True):
                return
    say(f"  Agora, no Telegram: adicione @{bot} ao grupo que vai receber os relatórios e envie lá:")
    say(_c("1", f"    /start@{bot}"))
    while True:
        input("  Quando tiver enviado, aperte Enter... ")
        chats, people = telegram_scan(v["TELEGRAM_BOT_TOKEN"])
        if chats:
            break
        fail("o bot ainda não viu nenhuma mensagem. Confira se ele está no grupo e envie /start@" + bot)
    chat_opts = [(str(cid), f"{title} (id {cid})") for cid, title in chats.items()]
    v["TELEGRAM_CHAT_ID"] = choose("Para qual conversa enviar os relatórios?", chat_opts, v.get("TELEGRAM_CHAT_ID"))
    if yes("Qualquer membro do grupo pode aprovar o envio ao Slack?", not v.get("TELEGRAM_APPROVER_IDS")):
        v["TELEGRAM_APPROVER_IDS"] = ""
    else:
        for pid, name in people.items():
            say(_c("90", f"    {pid} — {name}"))
        v["TELEGRAM_APPROVER_IDS"] = ask("IDs de quem pode aprovar (separados por vírgula)",
                                         v.get("TELEGRAM_APPROVER_IDS", ""))


def _menu_telegram(v: dict[str, str]) -> None:
    last = "guided"
    while True:
        approvers = v.get("TELEGRAM_APPROVER_IDS") or "qualquer membro"
        pick = choose("Telegram", [
            ("guided", "Configurar bot e descobrir grupo automaticamente"),
            ("token", f"Token do bot                [{mask(v.get('TELEGRAM_BOT_TOKEN', ''))}]"),
            ("chat", f"ID da conversa / grupo      [{v.get('TELEGRAM_CHAT_ID') or '(vazio)'}]"),
            ("approvers", f"Quem aprova envio ao Slack  [{approvers}]"),
            ("disable", "Desativar Telegram (usar só simulação)"),
            (_BACK, "← Voltar ao menu principal"),
        ], last, allow_esc=True)
        if pick == _BACK:
            return
        last = pick
        if pick == "guided":
            _setup_telegram_guided(v)
        elif pick == "token":
            v["TELEGRAM_BOT_TOKEN"] = ask_secret("Token do bot", v.get("TELEGRAM_BOT_TOKEN", ""))
        elif pick == "chat":
            v["TELEGRAM_CHAT_ID"] = ask("ID do chat/grupo (ex.: -1001234567890)", v.get("TELEGRAM_CHAT_ID", ""))
        elif pick == "approvers":
            mode = choose("Quem pode aprovar o envio ao Slack?", [
                ("all", "Qualquer membro do grupo"),
                ("list", "Apenas IDs específicos"),
                (_BACK, "← Voltar"),
            ], "all" if not v.get("TELEGRAM_APPROVER_IDS") else "list", allow_esc=True)
            if mode == "all":
                v["TELEGRAM_APPROVER_IDS"] = ""
            elif mode == "list":
                v["TELEGRAM_APPROVER_IDS"] = ask("IDs autorizados (separados por vírgula)",
                                                 v.get("TELEGRAM_APPROVER_IDS", ""))
        elif pick == "disable":
            v["TELEGRAM_BOT_TOKEN"] = ""
            v["TELEGRAM_CHAT_ID"] = ""
            v["TELEGRAM_APPROVER_IDS"] = ""
            ok("Telegram desativado")


def _menu_slack(v: dict[str, str]) -> None:
    last = "token"
    while True:
        pick = choose("Slack (opcional)", [
            ("token", f"Token do app (xoxb-...)     [{mask(v.get('SLACK_BOT_TOKEN', ''))}]"),
            ("channel", f"ID do canal                 [{v.get('SLACK_CHANNEL_ID') or '(vazio)'}]"),
            ("details", f"Enviar descrição completa   [{_on_off(v['SLACK_INCLUDE_DETAILS'])}]  (Enter alterna)"),
            ("disable", "Desativar Slack"),
            (_BACK, "← Voltar ao menu principal"),
        ], last, allow_esc=True)
        if pick == _BACK:
            return
        last = pick
        if pick == "token":
            token = ask_secret("Token do app (começa com xoxb-)", v.get("SLACK_BOT_TOKEN", ""))
            good, msg = check_slack(token)
            (ok if good else fail)(msg)
            v["SLACK_BOT_TOKEN"] = token
        elif pick == "channel":
            v["SLACK_CHANNEL_ID"] = ask(
                "ID do canal (ex.: C0123456789; detalhes do canal → fim da tela)",
                v.get("SLACK_CHANNEL_ID", ""),
                validate=lambda c: None if re.fullmatch(r"[CG][A-Z0-9]{6,}", c) else
                "o ID começa com C ou G e tem letras maiúsculas e números (não é o nome)",
            )
        elif pick == "details":
            _toggle(v, "SLACK_INCLUDE_DETAILS")
        elif pick == "disable":
            v["SLACK_BOT_TOKEN"] = ""
            v["SLACK_CHANNEL_ID"] = ""
            v["SLACK_INCLUDE_DETAILS"] = "false"
            ok("Slack desativado")


def _menu_agenda(v: dict[str, str]) -> None:
    last = "cron"
    while True:
        pick = choose("Agenda Automática (`check run`)", [
            ("cron", f"Horário das revisões        [{_short_cron(v['CRON_EXPR'])}]"),
            ("tz", f"Fuso horário                [{v['TZ']}]"),
            (_BACK, "← Voltar ao menu principal"),
        ], last, allow_esc=True)
        if pick == _BACK:
            return
        last = pick
        if pick == "cron":
            known = {val for val, _ in SCHEDULES if val != "custom"}
            cur_cron = v["CRON_EXPR"] if v["CRON_EXPR"] in known else "custom"
            cron = choose("Quando revisar?", [*SCHEDULES, (_BACK, "← Voltar")], cur_cron, allow_esc=True)
            if cron == "custom":
                from croniter import croniter
                v["CRON_EXPR"] = ask("Expressão cron (minuto hora dia mês dia-da-semana)", v["CRON_EXPR"],
                                     validate=lambda c: None if croniter.is_valid(c) else "expressão cron inválida")
            elif cron != _BACK:
                v["CRON_EXPR"] = cron
        elif pick == "tz":
            tz_opts = [*TIMEZONES, (_CUSTOM, "✎ Digitar outro fuso..."), (_BACK, "← Voltar")]
            pick_tz = choose("Fuso horário", tz_opts, v["TZ"], allow_esc=True)
            if pick_tz == _CUSTOM:
                v["TZ"] = ask("Fuso horário (ex.: America/Sao_Paulo)", v["TZ"])
            elif pick_tz != _BACK:
                v["TZ"] = pick_tz


def _menu_avancado(v: dict[str, str]) -> None:
    last = "chunk"
    while True:
        pick = choose("Limites Técnicos Avançados", [
            ("chunk", f"Tamanho de bloco/parte      [{v['CHUNK_CHARS']} caracteres]"),
            ("tokens", f"Tokens máximos de resposta  [{v['MAX_OUTPUT_TOKENS']} tokens]"),
            ("max_chunks", f"Máx. blocos no modo diff    [{v['MAX_CHUNKS']} blocos]"),
            ("lookback", f"Commits iniciais (diff)     [{v['INITIAL_LOOKBACK_COMMITS']} commits]"),
            ("data_dir", f"Pasta local de dados        [{v['DATA_DIR']}]"),
            ("reset", "Restaurar limites avançados para o padrão"),
            (_BACK, "← Voltar ao menu principal"),
        ], last, allow_esc=True)
        if pick == _BACK:
            return
        last = pick
        if pick == "chunk":
            v["CHUNK_CHARS"] = choose_number("Tamanho máximo de cada parte/bloco (caracteres)", v["CHUNK_CHARS"], [
                ("30000", "30.000 caracteres — partes menores"),
                ("60000", "60.000 caracteres — padrão"),
                ("100000", "100.000 caracteres — partes maiores"),
                ("150000", "150.000 caracteres"),
            ], 5000, 500000, "caracteres")
        elif pick == "tokens":
            v["MAX_OUTPUT_TOKENS"] = choose_number("Tokens máximos da resposta da IA (inclui raciocínio)",
                                                   v["MAX_OUTPUT_TOKENS"], [
                                                       ("16000", "16.000 tokens"),
                                                       ("32000", "32.000 tokens — padrão"),
                                                       ("64000", "64.000 tokens — respostas longas"),
                                                   ], 4096, 128000, "tokens")
        elif pick == "max_chunks":
            v["MAX_CHUNKS"] = choose_number("Modo diff: máximo de blocos de diff por execução", v["MAX_CHUNKS"], [
                ("3", "3 blocos"),
                ("6", "6 blocos — padrão"),
                ("12", "12 blocos"),
                ("25", "25 blocos"),
            ], 1, 100, "blocos")
        elif pick == "lookback":
            v["INITIAL_LOOKBACK_COMMITS"] = choose_number(
                "Modo diff: quantos commits olhar na 1ª execução", v["INITIAL_LOOKBACK_COMMITS"], [
                    ("10", "10 commits"),
                    ("20", "20 commits — padrão"),
                    ("50", "50 commits"),
                    ("100", "100 commits"),
                ], 1, 500, "commits",
            )
        elif pick == "data_dir":
            v["DATA_DIR"] = ask("Pasta local onde ficam o histórico e o cache", v["DATA_DIR"])
        elif pick == "reset":
            for k in ("CHUNK_CHARS", "MAX_OUTPUT_TOKENS", "MAX_CHUNKS", "INITIAL_LOOKBACK_COMMITS", "DATA_DIR"):
                v[k] = DEFAULTS[k]
            ok("Limites avançados restaurados para o padrão")


def _quick_initial_setup(v: dict[str, str]) -> None:
    """Fluxo rápido para quem abre `check configurar` pela primeira vez (preenche o essencial)."""
    say()
    say(_c("1;36", "━━ Configuração inicial rápida ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"))
    _menu_projeto(v)
    if not v.get("REPO_URL"):
        v["REPO_URL"] = ask("URL do repositório (ex.: https://github.com/empresa/projeto.git)", "",
                            validate=lambda u: None if re.match(r"(https://|git@)", u) else "comece com https:// ou git@")
        v["REPO_BRANCH"] = ask("Branch a revisar", v["REPO_BRANCH"])
        _setup_repo_auth(v)
        say(_c("90", "  testando o acesso..."))
        good, msg = check_repo(v["REPO_URL"], v["REPO_BRANCH"], v.get("REPO_TOKEN", ""), v.get("REPO_SSH_KEY", ""))
        (ok if good else fail)(msg)
    if not v.get("ANTHROPIC_API_KEY"):
        m = choose("Modelo de IA", MODELS, v["REVIEW_MODEL"])
        v["REVIEW_MODEL"] = m
        v["ANTHROPIC_API_KEY"] = ask_secret("Chave da Anthropic (começa com sk-ant-)")
        say(_c("90", "  testando a chave..."))
        good, msg = check_anthropic(v["ANTHROPIC_API_KEY"], v["REVIEW_MODEL"])
        (ok if good else fail)(msg)
    if not v.get("TELEGRAM_BOT_TOKEN") and yes("Configurar o Telegram agora?", True):
        _setup_telegram_guided(v)


# ---------------------------------------------------------------- menu principal

def _initial_state(cur: dict[str, str]) -> dict[str, str]:
    v = dict(DEFAULTS)
    for k, val in cur.items():
        if val or k not in DEFAULTS or not DEFAULTS[k]:
            v[k] = val
    return v


def run_wizard() -> None:
    if not sys.stdin.isatty():
        raise SystemExit("O assistente precisa de um terminal interativo. Rode `check configurar` direto no terminal.")

    v = _initial_state(read_env())

    say()
    say(_c("1", "Painel de configuração do revisor de segurança"))
    say(_c("90", "  Selecione qualquer item para ajustar e volte ao menu quando quiser."))
    say(_c("90", "  Nada é gravado até você escolher 'Salvar alterações e sair'."))

    last = "ritmo"
    while True:
        missing_core = not v.get("REPO_URL") or not v.get("ANTHROPIC_API_KEY")
        menu: list[tuple[str, str]] = []
        if missing_core:
            menu.append(("wizard", "⚡ Configuração guiada inicial — preenche o básico passo a passo"))
        menu.extend([
            ("ritmo", f"Ritmo, Unidades e Voltas   [{v['MAX_UNITS']} partes/vez · {v['MAX_TURNS']} voltas · modo {v['REVIEW_MODE']}]"),
            ("ia", f"Inteligência Artificial    [{_short_model(v['REVIEW_MODEL'])} · esforço {v['EFFORT']} · chave {mask(v.get('ANTHROPIC_API_KEY', ''))}]"),
            ("projeto", f"Projeto e Perfil           [{v['PROFILE']}]"),
            ("repo", f"Repositório Git            [{_short_repo(v)}]"),
            ("telegram", f"Telegram                   [{_short_telegram(v)}]"),
            ("slack", f"Slack (opcional)           [{_short_slack(v)}]"),
            ("agenda", f"Agenda Automática          [{_short_cron(v['CRON_EXPR'])} · {v['TZ']}]"),
            ("avancado", f"Limites Técnicos Avançados [bloco {v['CHUNK_CHARS']} car · saída {v['MAX_OUTPUT_TOKENS']} tok]"),
            ("save", "💾 Salvar alterações e sair"),
            ("quit", "✖ Sair sem salvar"),
        ])

        pick = choose("Menu de Configuração", menu, last if any(k == last for k, _ in menu) else menu[0][0])
        if pick == "quit":
            raise SystemExit("Configuração cancelada. Nada foi alterado.")
        if pick == "save":
            backup = write_env(v)
            ok(f"configuração salva em .env (só você consegue ler o arquivo)"
               f"{f'; anterior guardada em {backup.name}' if backup else ''}")
            say()
            say(_c("1", "Próximos passos"))
            say("  check verificar   confere tudo de novo (gratuito)")
            say("  check simular     primeira revisão, só na tela (tem custo de IA)")
            say("  check             revisão real, com relatório no Telegram")
            return

        last = pick
        if pick == "wizard":
            _quick_initial_setup(v)
        elif pick == "ritmo":
            _menu_ritmo(v)
        elif pick == "ia":
            _menu_ia(v)
        elif pick == "projeto":
            _menu_projeto(v)
        elif pick == "repo":
            _menu_repo(v)
        elif pick == "telegram":
            _menu_telegram(v)
        elif pick == "slack":
            _menu_slack(v)
        elif pick == "agenda":
            _menu_agenda(v)
        elif pick == "avancado":
            _menu_avancado(v)


# ---------------------------------------------------------------- diagnóstico

def run_doctor() -> bool:
    """Confere configuração e conexões sem gastar créditos de IA. Retorna True se não houver falha."""
    from dotenv import load_dotenv
    load_dotenv(ENV)
    env = os.environ
    problems = 0

    def report(good: bool, text: str, hint: str = "") -> None:
        nonlocal problems
        (ok if good else fail)(text)
        if not good:
            problems += 1
            if hint:
                say(_c("33", f"    → {hint}"))

    ui.step("Diagnóstico (sem custo de IA)")
    report(sys.version_info >= (3, 11), f"Python {sys.version.split()[0]}", "é preciso Python 3.11 ou mais novo")
    if not ENV.is_file():
        report(False, "arquivo .env não encontrado", "rode `check configurar`")
        return False
    mode = stat.S_IMODE(ENV.stat().st_mode)
    report(mode & 0o077 == 0, f".env com permissão {oct(mode)}", "rode: chmod 600 .env (só você deve conseguir ler)")

    try:
        p = profiles.load(env.get("PROFILE", "exemplo"))
        report(True, f"perfil '{p.name}' ({p.project}) carregado")
        if p.name == "exemplo":
            say(_c("33", "    → você está usando o perfil de exemplo; crie o seu com `check configurar`"))
    except SystemExit as exc:
        report(False, f"perfil: {exc}")

    if env.get("REPO_URL"):
        good, msg = check_repo(env["REPO_URL"], env.get("REPO_BRANCH", "main"), env.get("REPO_TOKEN", ""),
                               env.get("REPO_SSH_KEY", ""))
        report(good, msg, "confira REPO_URL e a credencial com `check configurar`")
    else:
        report(False, "REPO_URL não configurado", "rode `check configurar`")

    if env.get("ANTHROPIC_API_KEY"):
        good, msg = check_anthropic(env["ANTHROPIC_API_KEY"], env.get("REVIEW_MODEL", "claude-opus-5-5"))
        report(good, msg, "gere outra chave em console.anthropic.com e rode `check configurar`")
    else:
        report(False, "ANTHROPIC_API_KEY não configurada", "rode `check configurar`")

    token = env.get("TELEGRAM_BOT_TOKEN", "")
    if token:
        try:
            bot = telegram_call(token, "getMe")
            report(True, f"Telegram: bot @{bot['username']}")
            chat = env.get("TELEGRAM_CHAT_ID", "")
            member = telegram_call(token, "getChatMember", chat_id=int(chat), user_id=bot["id"]) if chat else {}
            in_chat = member.get("status") in ("member", "administrator", "creator")
            report(in_chat, f"Telegram: bot no chat {chat or '(não configurado)'}",
                   "adicione o bot ao grupo e rode `check configurar`")
        except Exception as exc:
            report(False, f"Telegram: {str(exc)[:100]}", "confira o token e o grupo com `check configurar`")
    else:
        say(_c("90", "  · Telegram não configurado (só o modo simulação funciona)"))

    if env.get("SLACK_BOT_TOKEN"):
        good, msg = check_slack(env["SLACK_BOT_TOKEN"])
        report(good, f"Slack: {msg}", "gere um novo token e rode `check configurar`")
        report(bool(env.get("SLACK_CHANNEL_ID")), "Slack: canal configurado", "informe o ID do canal")
    else:
        say(_c("90", "  · Slack não configurado (o botão do Slack não aparece no Telegram)"))

    if env.get("DEPS_CHECK", "true") == "true":
        try:
            httpx.post("https://api.osv.dev/v1/querybatch", json={"queries": []}, timeout=20).raise_for_status()
            report(True, "osv.dev acessível (checagem de dependências)")
        except httpx.HTTPError:
            report(False, "osv.dev inacessível", "sem internet? ou desligue com DEPS_CHECK=false")

    link = Path.home() / ".local/bin/check"
    report(link.exists(), "atalho `check` instalado", "rode ./install.sh de novo")
    say()
    if problems:
        say(_c("31", f"{problems} problema(s) encontrado(s)."))
    else:
        say(_c("32", "Tudo certo. Próximo passo: `check simular`."))
    return problems == 0
