"""Traduz exceções em (título, detalhe, o que fazer) em português e oculta segredos."""
import re

import anthropic

from .gitrepo import NETWORK_ERROR, GitError

_SECRETS = [
    (re.compile(r"bot\d{6,12}:[A-Za-z0-9_-]{20,}"), "bot<oculto>"),   # token do Telegram dentro da URL da API
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}"), "<token-oculto>"),  # o mesmo token, solto no texto
    (re.compile(r"sk-ant-[A-Za-z0-9_-]+"), "sk-ant-<oculto>"),        # chave da Anthropic
    (re.compile(r"xox[a-z]-[A-Za-z0-9-]+"), "xox<oculto>"),           # token do Slack
    (re.compile(r"(?i)(authorization: *(?:basic|bearer) +)\S+"), r"\1<oculto>"),
    (re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@"), r"\1<oculto>@"),  # credencial embutida em URL
]


class ModelRefusal(RuntimeError):
    """O modelo (e o fallback) recusaram o pedido pelos filtros de segurança da Anthropic."""

    def __init__(self, category: str):
        super().__init__(f"recusa do modelo (categoria: {category})")
        self.category = category


_NETWORK_LIBS = {"httpx", "httpx2", "httpcore", "httpcore2"}


def is_network_error(exc: BaseException) -> bool:
    """Falha de transporte (timeout, conexão caída). O SDK usa httpx2, então reconhece pelo módulo."""
    return (type(exc).__module__.split(".")[0] in _NETWORK_LIBS
            or isinstance(exc, (TimeoutError, ConnectionError)))


def is_transient(exc: BaseException) -> bool:
    """Vale repetir a chamada: rede caída, API sobrecarregada ou erro 5xx."""
    return is_network_error(exc) or isinstance(
        exc, (anthropic.APIConnectionError, anthropic.APITimeoutError, anthropic.OverloadedError,
              anthropic.ServiceUnavailableError, anthropic.InternalServerError))


def redact(text: str) -> str:
    for pattern, repl in _SECRETS:
        text = pattern.sub(repl, text)
    return text


def _msg(exc: Exception, limit: int = 300) -> str:
    return redact(str(exc)).strip()[:limit]


def explain(exc: BaseException) -> tuple[str, str, str]:
    """Retorna (título, detalhe, o que fazer)."""
    text = str(exc)

    # --- API da Anthropic
    if isinstance(exc, ModelRefusal) and exc.category == "reasoning_extraction":
        return ("O modelo recusou o pedido como extração do próprio raciocínio (reasoning_extraction)", "",
                "É um problema de prompt do agente, não do código revisado: algum prompt pede o raciocínio na resposta. "
                "Esse tipo de recusa não passa para o modelo alternativo. Rode com --debug e me envie data/last-error.log.")
    if isinstance(exc, ModelRefusal):
        return (f"O modelo recusou analisar este trecho (categoria: {exc.category})", "",
                "Os filtros de segurança da Anthropic às vezes barram código de segurança; a tentativa num modelo "
                "alternativo também foi recusada. A unidade será tentada de novo na próxima execução.")
    if isinstance(exc, anthropic.AuthenticationError):
        return ("Chave da Anthropic recusada (401)", "",
                "Confira ANTHROPIC_API_KEY no .env. A chave pode ter sido revogada ou copiada incompleta.")
    if isinstance(exc, anthropic.PermissionDeniedError):
        return ("A Anthropic negou a permissão (403)", _msg(exc),
                "Verifique se a chave tem acesso ao modelo em REVIEW_MODEL (console.anthropic.com).")
    if isinstance(exc, anthropic.RateLimitError):
        return ("Limite de uso da API da Anthropic atingido (429)", "",
                "Aguarde alguns minutos e rode de novo. Se repetir, reduza MAX_UNITS ou MAX_TURNS no .env.")
    if isinstance(exc, anthropic.BadRequestError):
        if re.search(r"credit balance|billing|credits", text, re.I):
            return ("Saldo de créditos da Anthropic insuficiente", "",
                    "Adicione créditos em console.anthropic.com → Plans & Billing e rode de novo.")
        return ("A API recusou o pedido (400)", _msg(exc),
                "Se mencionar o tamanho, reduza CHUNK_CHARS. Rode com --debug para ver o detalhe técnico.")
    if isinstance(exc, anthropic.RequestTooLargeError):
        return ("Pedido grande demais para a API", "", "Reduza CHUNK_CHARS no .env.")
    if isinstance(exc, (anthropic.OverloadedError, anthropic.ServiceUnavailableError, anthropic.InternalServerError)):
        return ("A API da Anthropic está instável ou sobrecarregada", "",
                "Não é problema do agente. Tente de novo em alguns minutos (o agente já tentou 3 vezes).")
    if isinstance(exc, anthropic.APITimeoutError):
        return ("A Anthropic demorou demais para responder", "",
                "Tente de novo. Se repetir, reduza CHUNK_CHARS para a unidade ser menor.")
    if isinstance(exc, anthropic.APIConnectionError):
        return ("Sem conexão com a API da Anthropic", "",
                "Verifique a internet/VPN. Teste: curl -4 -I https://api.anthropic.com")

    # --- Git / GitHub
    if isinstance(exc, GitError):
        if re.search(r"Authentication failed|could not read Username|Permission denied|Repository not found|"
                     r"publickey|403|401", text, re.I):
            return ("O GitHub recusou o acesso ao repositório", _msg(exc),
                    "Confira REPO_URL e a credencial (REPO_TOKEN ou REPO_SSH_KEY). Ela precisa de leitura no repositório.")
        if re.search(r"couldn't find remote ref|Remote branch .* not found", text, re.I):
            return ("A branch não existe no repositório", _msg(exc), "Confira REPO_BRANCH no .env.")
        if NETWORK_ERROR.search(text):
            return ("Sem acesso ao GitHub (problema de rede)", "",
                    "O agente já tentou 3 vezes. Teste: curl -4 -I https://github.com. "
                    "Se falhar, verifique VPN/firewall ou rode `wsl --shutdown` no PowerShell.")
        return ("Falha ao executar o git", _msg(exc), "Rode com --debug para ver o detalhe técnico.")

    # --- Telegram / Slack (os clientes levantam RuntimeError com a descrição da API)
    if isinstance(exc, RuntimeError) and text.startswith("Telegram"):
        hint = "Confira TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID."
        if re.search(r"kicked|not a member|bot was blocked|chat not found|Forbidden", text, re.I):
            hint = "O bot não está no grupo (ou foi removido). Adicione o bot ao grupo de novo e confira TELEGRAM_CHAT_ID."
        elif re.search(r"Unauthorized|401", text, re.I):
            hint = "O token do bot foi recusado. Confira TELEGRAM_BOT_TOKEN (pode ter sido revogado no BotFather)."
        elif re.search(r"Too Many Requests|retry after", text, re.I):
            hint = "O Telegram limitou os envios. Aguarde um minuto e rode de novo."
        return ("O Telegram recusou o envio", _msg(exc), hint)
    if isinstance(exc, RuntimeError) and text.startswith("Slack"):
        hint = "Confira SLACK_BOT_TOKEN e SLACK_CHANNEL_ID."
        if re.search(r"not_in_channel|channel_not_found", text):
            hint = "Convide o app para o canal (/invite @nome-do-app) e confira SLACK_CHANNEL_ID (use o ID, não o nome)."
        elif re.search(r"invalid_auth|token_revoked|not_authed", text):
            hint = "O token do Slack foi recusado. Gere um novo e atualize SLACK_BOT_TOKEN."
        return ("O Slack recusou a mensagem", _msg(exc), hint)
    if is_network_error(exc):
        what = "expirou o tempo de resposta" if "timed out" in text.lower() else "a conexão foi interrompida"
        return (f"Falha de rede: {what}", "",
                "A internet oscilou durante uma chamada (Anthropic, Telegram ou Slack). O agente já tentou de novo. "
                "Rode outra vez; se repetir, teste: curl -4 -I https://api.anthropic.com e verifique VPN/firewall.")

    # --- erros que o próprio agente levanta com mensagem já clara
    if isinstance(exc, RuntimeError) and "max_tokens" in text:
        return ("A resposta do modelo foi cortada", _msg(exc), "Aumente MAX_OUTPUT_TOKENS ou reduza CHUNK_CHARS no .env.")
    if isinstance(exc, RuntimeError):
        return ("A revisão não terminou", _msg(exc), "Rode de novo. Se repetir, use --debug e me envie data/last-error.log.")

    return (f"Erro inesperado ({type(exc).__name__})", _msg(exc),
            "Rode com --debug para ver o detalhe técnico. O registro completo fica em data/last-error.log.")
