"""Saída de terminal para acompanhar o agente trabalhando. Sem dependências."""
import os
import sys
import threading
import time

enabled = True
_tty = sys.stdout.isatty()
_color = _tty and "NO_COLOR" not in os.environ
_midline = False  # True enquanto há texto em streaming sem quebra de linha

_lock = threading.RLock()
_status: dict | None = None  # indicador de espera: {label, t0, chars}
_status_drawn = False  # há uma linha de status desenhada no terminal
_ticker: threading.Thread | None = None

RESET = "\033[0m"
SEV_COLOR = {"critical": "1;31", "high": "31", "medium": "33", "low": "34", "info": "90"}
SEV_LABEL = {"critical": "CRÍTICO", "high": "ALTO", "medium": "MÉDIO", "low": "BAIXO", "info": "INFO"}


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}{RESET}" if _color else text


def _clear_status() -> None:
    global _status_drawn
    if _status_drawn:
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()
        _status_drawn = False


def _print(text: str = "") -> None:
    global _midline
    if not enabled:
        return
    with _lock:
        _clear_status()
        if _midline:
            sys.stdout.write("\n")
            _midline = False
        print(text, flush=True)


# ---------------------------------------------------------------- indicador de espera

def _tick() -> None:
    """Redesenha 'aguardando o modelo… Ns' a cada meio segundo (ou a cada 15 s se não for um terminal)."""
    global _status_drawn
    last_plain = 0
    while True:
        time.sleep(0.5)
        with _lock:
            if _status is None or _midline or not enabled:
                continue
            elapsed = int(time.monotonic() - _status["t0"])
            chars = f" · {_status['chars']:,} caracteres".replace(",", ".") if _status["chars"] else ""
            line = f"⏳ {_status['label']} {elapsed}s{chars}"
            if _tty:
                sys.stdout.write("\r\033[K" + _c("33", line))
                sys.stdout.flush()
                _status_drawn = True
            elif elapsed - last_plain >= 15:
                print(line, flush=True)
                last_plain = elapsed


def wait_start(label: str) -> None:
    global _status, _ticker
    if not enabled:
        return
    with _lock:
        _status = {"label": label, "t0": time.monotonic(), "chars": 0}
    if _ticker is None or not _ticker.is_alive():
        _ticker = threading.Thread(target=_tick, daemon=True)
        _ticker.start()


def wait_label(label: str) -> None:
    """Troca o texto do indicador (ex.: quando o modelo começa a montar a resposta final)."""
    global _midline
    with _lock:
        if _status is None:
            return
        _status["label"] = label
        if _midline:  # termina a linha da fala do modelo para o indicador poder reaparecer
            sys.stdout.write("\n")
            _midline = False


def wait_progress(chars: int) -> None:
    with _lock:
        if _status is not None:
            _status["chars"] += chars


def wait_stop() -> None:
    global _status
    with _lock:
        _status = None
        _clear_status()


# ---------------------------------------------------------------- mensagens

def step(text: str) -> None:
    _print()
    _print(f"{_c('1;36', '▶')} {_c('1', text)}")


def info(text: str) -> None:
    _print(f"  {_c('90', text)}")


def warn(text: str) -> None:
    _print(f"  {_c('33', '⚠ ' + text)}")


def stream(text: str) -> None:
    """Texto do modelo chegando em tempo real (as falas entre uma ferramenta e outra)."""
    global _midline
    if not enabled or not text:
        return
    with _lock:
        _clear_status()
        sys.stdout.write(_c("2", text))
        sys.stdout.flush()
        _midline = not text.endswith("\n")


def thought(text: str) -> None:
    """Resumo do raciocínio do modelo (thinking display=summarized), em itálico apagado."""
    global _midline
    if not enabled or not text:
        return
    with _lock:
        _clear_status()
        if not _midline:
            sys.stdout.write(_c("2;3", "  💭 "))
        sys.stdout.write(_c("2;3", text))
        sys.stdout.flush()
        _midline = not text.endswith("\n")


def tool_call(name: str, args: dict) -> None:
    detail = ", ".join(f"{k}={str(v)[:70]!r}" for k, v in args.items())
    _print(f"  {_c('35', '🔎')} {_c('35', name)}({detail})")


def tool_result(summary: str) -> None:
    _print(f"     {_c('90', '↳ ' + summary)}")


def usage(stage: str, usage_obj, seconds: float | None = None) -> None:
    cached = getattr(usage_obj, "cache_read_input_tokens", 0) or 0
    took = f"{seconds:.0f}s · " if seconds is not None else ""
    _print(_c("90", f"  [{stage}] {took}tokens: entrada={usage_obj.input_tokens} (cache={cached}) "
                    f"saída={usage_obj.output_tokens}"))


def analysis(text: str) -> None:
    if not text.strip():
        return
    _print(f"  {_c('1', 'Resumo:')}")
    for line in text.strip().splitlines():
        _print(f"    {_c('2', line)}")


def finding(f: dict) -> None:
    sev = f["severity"]
    loc = f"{f['file']}:{f['line']}" if f.get("line") else f["file"]
    rule = f" [{f['regra']}]" if f.get("regra") else ""
    _print(f"  {_c(SEV_COLOR[sev], '■ ' + SEV_LABEL[sev])}{rule} {_c('1', f['title'])}")
    _print(f"    {_c('36', loc)}")


def dropped(f: dict, reason: str) -> None:
    _print(f"  {_c('90', '✗ descartado pela verificação: ' + f['title'])}")
    _print(f"    {_c('90', reason)}")


def done(text: str) -> None:
    _print()
    _print(f"{_c('1;32', '✔')} {_c('1', text)}")


def error(title: str, detail: str = "", hint: str = "") -> None:
    """Erro explicado em português. Vai para o stderr e aparece mesmo com --quiet."""
    wait_stop()
    with _lock:
        _clear_status()
        out = sys.stderr
        out.write(f"\n{_c('1;31', '✖ ' + title)}\n")
        if detail:
            out.write(f"  {detail}\n")
        if hint:
            out.write(f"  {_c('33', '→ O que fazer:')} {hint}\n")
        out.flush()
