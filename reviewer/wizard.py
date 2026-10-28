"""Assistente de configuração (`check configurar`) e diagnóstico (`check verificar`).

O assistente pergunta uma coisa por vez, valida cada credencial na hora (sem gastar créditos de IA) e só grava o
.env no final, com backup do anterior. Chaves são digitadas sem aparecer na tela e nunca são impressas.
"""
import getpass
import os
import re
import shutil
import stat
import subprocess
import sys
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
    ("custom", "Outro horário (expressão cron)"),
]


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


# ---------------------------------------------------------------- perguntas

def _c(code: str, text: str) -> str:
    return ui._c(code, text)


def say(text: str = "") -> None:
    print(text, flush=True)


def section(n: int, total: int, title: str, why: str) -> None:
    say()
    say(_c("1;36", f"━━ {n}/{total} · {title} " + "━" * max(3, 50 - len(title))))
    for line in why.strip().splitlines():
        say(_c("90", "  " + line.strip()))


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


def choose(label: str, options: list[tuple[str, str]], default: str | None = None) -> str:
    say(f"  {label}")
    for i, (value, text) in enumerate(options, 1):
        mark = _c("32", " (atual)") if value == default else ""
        say(f"    {i}) {text}{mark}")
    default_idx = next((str(i) for i, (v, _) in enumerate(options, 1) if v == default), "1")
    while True:
        pick = input(f"  Escolha [{default_idx}]: ").strip() or default_idx
        if pick.isdigit() and 1 <= int(pick) <= len(options):
            return options[int(pick) - 1][0]
        say(_c("33", f"  ⚠ digite um número de 1 a {len(options)}"))


def yes(label: str, default: bool = True) -> bool:
    hint = "S/n" if default else "s/N"
    answer = input(f"  {label} [{hint}]: ").strip().lower()
    return default if not answer else answer in ("s", "sim", "y", "yes")


def ok(text: str) -> None:
    say(_c("32", f"  ✓ {text}"))


def fail(text: str) -> None:
    say(_c("31", f"  ✗ {text}"))


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


# ---------------------------------------------------------------- assistente

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
        say(_c("33", "    Quanto melhor o contexto, melhor a revisão. O README explica cada arquivo."))
        return slug


def run_wizard() -> None:
    if not sys.stdin.isatty():
        raise SystemExit("O assistente precisa de um terminal interativo. Rode `check configurar` direto no terminal.")
    cur = read_env()
    v: dict[str, str] = dict(cur)
    total = 7

    say()
    say(_c("1", "Assistente de configuração do revisor de segurança"))
    say(_c("90", "  Responda uma pergunta por vez. Enter aceita o valor entre colchetes."))
    say(_c("90", "  Nada é salvo até o final. Ctrl+C cancela a qualquer momento, sem alterar nada."))
    if cur:
        say(_c("90", "  Já existe uma configuração: os valores atuais aparecem como padrão."))

    # 1. projeto e perfil
    section(1, total, "Projeto", """
        O perfil diz ao revisor o que é o seu sistema e quais regras conferir. Ele fica em profiles/<nome>/.
        O perfil "exemplo" serve para testar; para o seu projeto, crie um perfil próprio (fica só no seu PC).""")
    options = [(p, f"{p} — {profiles.load(p).project}") for p in profiles.available()]
    options.append(("__novo__", "Criar um perfil novo para o meu projeto (a partir do exemplo)"))
    pick = choose("Qual perfil usar?", options, cur.get("PROFILE", "exemplo"))
    v["PROFILE"] = _new_profile() if pick == "__novo__" else pick

    # 2. repositório
    section(2, total, "Repositório", """
        O revisor baixa uma CÓPIA do código e só lê (nunca escreve no repositório).
        Para repositório privado, ele precisa de uma credencial; o ideal é uma só de leitura.""")
    v["REPO_URL"] = ask("URL do repositório (ex.: https://github.com/empresa/projeto.git)", cur.get("REPO_URL", ""),
                        validate=lambda u: None if re.match(r"(https://|git@)", u) else "comece com https:// ou git@")
    v["REPO_BRANCH"] = ask("Branch a revisar", cur.get("REPO_BRANCH", "main"))
    method = choose("Como o revisor acessa o repositório?", [
        ("helper", "Do jeito que o git deste computador já acessa (mais simples; usa a sua credencial)"),
        ("token", "Token do GitHub só de leitura (recomendado: fine-grained, permissão Contents: Read)"),
        ("ssh", "Chave SSH só de leitura (deploy key)"),
        ("public", "Repositório público (sem credencial)"),
    ], "token" if cur.get("REPO_TOKEN") else "ssh" if cur.get("REPO_SSH_KEY") else "helper")
    v["REPO_TOKEN"], v["REPO_SSH_KEY"] = "", ""
    if method == "token":
        v["REPO_TOKEN"] = ask_secret("Token do GitHub", cur.get("REPO_TOKEN", ""))
    elif method == "ssh":
        v["REPO_SSH_KEY"] = ask("Caminho da chave privada", cur.get("REPO_SSH_KEY", str(Path.home() / ".ssh/revisor_deploy")),
                                validate=lambda p: None if Path(p).expanduser().is_file() else "arquivo não encontrado")
        if not v["REPO_URL"].startswith("git@"):
            say(_c("33", "  ⚠ com chave SSH, a URL precisa ser no formato git@github.com:empresa/projeto.git"))
    say(_c("90", "  testando o acesso..."))
    good, msg = check_repo(v["REPO_URL"], v["REPO_BRANCH"], v["REPO_TOKEN"], v["REPO_SSH_KEY"])
    (ok if good else fail)(msg)
    if not good and not yes("Continuar mesmo assim? (dá para corrigir depois)", False):
        raise SystemExit("Configuração cancelada. Nada foi alterado.")

    # 3. Anthropic
    section(3, total, "Inteligência artificial (Anthropic)", """
        A chave fica em console.anthropic.com → API Keys. Dica: crie uma chave só para o revisor e defina um
        limite de gasto mensal no console. A validação abaixo é gratuita.""")
    v["REVIEW_MODEL"] = choose("Modelo", MODELS, cur.get("REVIEW_MODEL", "claude-opus-5-5"))
    v["ANTHROPIC_API_KEY"] = ask_secret("Chave da Anthropic (começa com sk-ant-)", cur.get("ANTHROPIC_API_KEY", ""))
    say(_c("90", "  testando a chave..."))
    good, msg = check_anthropic(v["ANTHROPIC_API_KEY"], v["REVIEW_MODEL"])
    (ok if good else fail)(msg)
    v["EFFORT"] = choose("Profundidade da análise (esforço)", [
        ("low", "Baixa — mais rápido e barato"),
        ("medium", "Média — equilíbrio (recomendado)"),
        ("high", "Alta — mais minucioso e caro"),
    ], cur.get("EFFORT", "medium"))

    # 4. ritmo e custo
    section(4, total, "Quanto revisar por vez (e quanto custa)", """
        O código é dividido em partes (cada função, lotes de migrations, pastas do front) e revisado aos poucos.
        A cada execução, ele revisa N partes; o que não mudou não é revisado de novo.""")
    dual = yes("Segunda revisão com olhar de atacante nas partes críticas? (acha mais coisa, ~70% mais caro)",
               cur.get("DUAL_REVIEW", "critical") != "off")
    v["DUAL_REVIEW"] = "critical" if dual else "off"
    unit_cost = COST_PER_UNIT.get((v["REVIEW_MODEL"], dual), 0.8)
    for n in (2, 5, 10):
        say(_c("90", f"    {n:>2} partes por execução ≈ US$ {n * unit_cost:.2f} por execução".replace(".", ",")))
    v["MAX_UNITS"] = ask("Partes por execução", cur.get("MAX_UNITS", "5"),
                         validate=lambda x: None if x.isdigit() and 1 <= int(x) <= 100 else "um número de 1 a 100")
    v["DEPS_CHECK"] = "true" if yes("Conferir bibliotecas com falhas conhecidas no osv.dev? "
                                    "(gratuito; envia só nome e versão dos pacotes)",
                                    cur.get("DEPS_CHECK", "true") == "true") else "false"
    v.setdefault("REVIEW_MODE", "code")
    v.setdefault("VERIFY", "true")

    # 5. Telegram
    section(5, total, "Telegram (onde chegam os relatórios)", """
        Crie um bot com o @BotFather no Telegram (comando /newbot) e copie o token que ele der.
        Sem Telegram, dá para usar só o modo simulação (o relatório aparece no terminal).""")
    if yes("Configurar o Telegram agora?", bool(cur.get("TELEGRAM_BOT_TOKEN")) or not cur):
        while True:
            v["TELEGRAM_BOT_TOKEN"] = ask_secret("Token do bot", cur.get("TELEGRAM_BOT_TOKEN", ""))
            try:
                bot = telegram_call(v["TELEGRAM_BOT_TOKEN"], "getMe")["username"]
                ok(f"bot @{bot} encontrado")
                break
            except Exception as exc:
                fail(f"o Telegram recusou o token ({str(exc)[:80]})")
                cur["TELEGRAM_BOT_TOKEN"] = ""
        say(f"  Agora, no Telegram: adicione @{bot} ao grupo que vai receber os relatórios e envie lá a mensagem")
        say(_c("1", f"    /start@{bot}"))
        while True:
            input("  Quando tiver enviado, aperte Enter... ")
            chats, people = telegram_scan(v["TELEGRAM_BOT_TOKEN"])
            if chats:
                break
            fail("o bot ainda não viu nenhuma mensagem. Confira se ele está no grupo e envie /start@" + bot)
        chat_opts = [(str(cid), f"{title} (id {cid})") for cid, title in chats.items()]
        v["TELEGRAM_CHAT_ID"] = choose("Para qual conversa enviar os relatórios?", chat_opts, cur.get("TELEGRAM_CHAT_ID"))
        if yes("Qualquer membro do grupo pode aprovar o envio ao Slack?", not cur.get("TELEGRAM_APPROVER_IDS")):
            v["TELEGRAM_APPROVER_IDS"] = ""
        else:
            for pid, name in people.items():
                say(_c("90", f"    {pid} — {name}"))
            v["TELEGRAM_APPROVER_IDS"] = ask("IDs de quem pode aprovar (separados por vírgula)",
                                             cur.get("TELEGRAM_APPROVER_IDS", ""))
    else:
        say(_c("33", "  → Sem Telegram: use `check simular`. Para configurar depois, rode `check configurar`."))

    # 6. Slack
    section(6, total, "Slack (opcional)", """
        Se configurado, aparece um botão no Telegram para mandar um resumo ao canal do Slack.
        Precisa de um app do Slack com permissão chat:write, convidado para o canal (/invite @nome-do-app).""")
    if yes("Configurar o Slack?", bool(cur.get("SLACK_BOT_TOKEN"))):
        v["SLACK_BOT_TOKEN"] = ask_secret("Token do app (começa com xoxb-)", cur.get("SLACK_BOT_TOKEN", ""))
        good, msg = check_slack(v["SLACK_BOT_TOKEN"])
        (ok if good else fail)(msg)
        v["SLACK_CHANNEL_ID"] = ask("ID do canal (ex.: C0123456789; no Slack: detalhes do canal → fim da tela)",
                                    cur.get("SLACK_CHANNEL_ID", ""),
                                    validate=lambda c: None if re.fullmatch(r"[CG][A-Z0-9]{6,}", c) else
                                    "o ID começa com C ou G e tem letras maiúsculas e números (não é o nome)")
        details = yes("Mandar a descrição dos achados ao Slack? (Não = só título e arquivo)",
                      cur.get("SLACK_INCLUDE_DETAILS", "false") == "true")
        v["SLACK_INCLUDE_DETAILS"] = "true" if details else "false"
    else:
        v["SLACK_BOT_TOKEN"], v["SLACK_CHANNEL_ID"] = "", ""

    # 7. agenda
    section(7, total, "Agenda (modo automático)", """
        Vale para `check run`, que fica rodando e revisa sozinho no horário escolhido.
        Se o computador estiver desligado no horário, ele roda a revisão perdida quando voltar.""")
    cron = choose("Quando revisar?", SCHEDULES, cur.get("CRON_EXPR", "0 8 * * *")
                  if cur.get("CRON_EXPR") in dict(SCHEDULES) else "0 8 * * *")
    if cron == "custom":
        from croniter import croniter
        cron = ask("Expressão cron (minuto hora dia mês dia-da-semana)", cur.get("CRON_EXPR", "0 8 * * *"),
                   validate=lambda c: None if croniter.is_valid(c) else "expressão cron inválida")
    v["CRON_EXPR"] = cron
    v["TZ"] = ask("Fuso horário", cur.get("TZ", "America/Sao_Paulo"))

    # resumo
    say()
    say(_c("1", "Resumo"))
    secret = {"ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "SLACK_BOT_TOKEN", "REPO_TOKEN"}
    for key in ("PROFILE", "REPO_URL", "REPO_BRANCH", "REPO_TOKEN", "REPO_SSH_KEY", "REVIEW_MODEL", "ANTHROPIC_API_KEY",
                "EFFORT", "DUAL_REVIEW", "MAX_UNITS", "DEPS_CHECK", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                "TELEGRAM_APPROVER_IDS", "SLACK_BOT_TOKEN", "SLACK_CHANNEL_ID", "CRON_EXPR", "TZ"):
        shown = mask(v.get(key, "")) if key in secret else (v.get(key) or "(vazio)")
        say(f"  {key:<22} {shown}")
    if not yes("Salvar esta configuração?", True):
        raise SystemExit("Configuração cancelada. Nada foi alterado.")
    backup = write_env(v)
    ok(f"configuração salva em .env (só você consegue ler o arquivo){f'; anterior guardada em {backup.name}' if backup else ''}")
    say()
    say(_c("1", "Próximos passos"))
    say("  check verificar   confere tudo de novo (gratuito)")
    say("  check simular     primeira revisão, só na tela (tem custo de IA)")
    say("  check             revisão real, com relatório no Telegram")


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
