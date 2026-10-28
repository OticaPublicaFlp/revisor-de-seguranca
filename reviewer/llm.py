import json
import re
import time
from pathlib import Path

import anthropic

from . import errors, tools, ui
from .config import ROOT, Config

SEVERITIES = ["critical", "high", "medium", "low", "info"]

REPORT_FINDINGS = {
    "name": "report_findings",
    "description": "Entrega o resultado da revisão. Use `findings` vazio quando o código estiver correto.",
    "input_schema": {
        "type": "object",
        "properties": {
            # Só um resumo do que foi examinado. Pedir o raciocínio na resposta é recusado pelo Opus 5.5
            # (reasoning_extraction): o raciocínio fica no thinking, que o terminal mostra resumido.
            "resumo": {"type": "string", "description": "Resumo curto do que foi examinado e concluído: arquivos lidos, "
                                                         "regras aplicadas e suspeitas descartadas, em até 5 linhas"},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "regra": {"type": "string", "description": "ID da regra do checklist (ex.: EF-2, DB-1)"},
                        "severity": {"type": "string", "enum": SEVERITIES},
                        "file": {"type": "string"},
                        "line": {"type": ["integer", "null"]},
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "suggestion": {"type": "string"},
                    },
                    "required": ["regra", "severity", "file", "title", "description", "suggestion"],
                },
            },
            "verificados": {"type": "array", "items": {"type": "string"},
                            "description": "Itens conferidos sem problema, no formato 'caminho/do/arquivo — REGRA: frase'"},
        },
        "required": ["resumo", "findings", "verificados"],
    },
}

REPORT_VERDICTS = {
    "name": "report_verdicts",
    "description": "Entrega um veredito para cada achado candidato.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer", "minimum": 0},
                        "verdict": {"type": "string", "enum": ["confirmed", "refuted", "uncertain"]},
                        "severity": {"type": "string", "enum": SEVERITIES},
                        "motivo": {"type": "string"},
                    },
                    "required": ["index", "verdict", "motivo"],
                },
            }
        },
        "required": ["verdicts"],
    },
}


_SECTION = re.compile(r"(?m)^#### ")


def rules_text(full: str, groups: frozenset[str] | None) -> str:
    """Checklist (regras.md do perfil) com só as seções pedidas (ex.: {'EF','DB'}). None = checklist completo.

    O cabeçalho e a seção "Fora do escopo" vão sempre: menos regras irrelevantes = revisão mais focada.
    """
    if groups is None:
        return full
    head, *sections = _SECTION.split(full)
    kept = [s for s in sections
            if s.split(" ", 1)[0] in groups or s.startswith("Fora do escopo")]
    return head + "".join(f"#### {s}" for s in kept)


FALLBACK_BETA = "server-side-fallback-2026-07-01"

# US$ por milhão de tokens (tabela da Anthropic de 2026-10). Escrita em cache = 1,25x a entrada.
PRICES = {
    "claude-opus-5-5": {"in": 4.00, "out": 20.00, "cache_read": 0.20, "cache_write": 5.00},
    "claude-sonnet-5-5": {"in": 2.00, "out": 10.00, "cache_read": 0.20, "cache_write": 2.50},
}


def make_client(cfg: Config) -> anthropic.Anthropic:
    # read=90: a API manda 'ping' durante a geração; 90 s sem nenhum dado = conexão morta. Falha e é refeita
    # (em vez de esperar para sempre). Usa anthropic.Timeout: o SDK tem o próprio httpx.
    return anthropic.Anthropic(api_key=cfg.anthropic_api_key, max_retries=3,
                               timeout=anthropic.Timeout(90.0, connect=15.0))


def _prompt(cfg: Config, name: str, groups: frozenset[str] | None = None) -> list[dict]:
    """System prompt = modelo genérico da etapa (prompts/) + contexto, exemplos e checklist do perfil.
    Em cache: é repetido a cada volta."""
    p = cfg.profile
    text = ((ROOT / "prompts" / f"{name}.md").read_text(encoding="utf-8")
            .replace("{{PROJETO}}", p.project)
            .replace("{{CONTEXTO}}", p.context)
            .replace("{{EXEMPLOS}}", p.examples or "(este perfil não tem exemplos)"))
    return [{"type": "text", "text": f"{text}\n\n{rules_text(p.rules, groups)}",
             "cache_control": {"type": "ephemeral"}}]


def _summary(text: str) -> str:
    if text.startswith("erro:") or len(text) < 60:
        return text
    return f"{len(text.splitlines())} linhas, {len(text)} caracteres"


STATS = {"turns": 0, "in": 0, "cache": 0, "cache_write": 0, "out": 0, "seconds": 0.0, "usd": 0.0}


def reset_stats() -> None:
    for k in STATS:
        STATS[k] = 0.0 if k in ("seconds", "usd") else 0


def _account(model: str, usage, seconds: float) -> None:
    cached = getattr(usage, "cache_read_input_tokens", 0) or 0
    written = getattr(usage, "cache_creation_input_tokens", 0) or 0
    STATS["turns"] += 1
    STATS["in"] += usage.input_tokens
    STATS["cache"] += cached
    STATS["cache_write"] += written
    STATS["out"] += usage.output_tokens
    STATS["seconds"] += seconds
    price = PRICES.get(model)
    if price:
        STATS["usd"] += (usage.input_tokens * price["in"] + cached * price["cache_read"]
                         + written * price["cache_write"] + usage.output_tokens * price["out"]) / 1_000_000


def stats_summary() -> str:
    cost = f" · custo estimado ≈ US$ {STATS['usd']:.2f}".replace(".", ",") if STATS["usd"] else ""
    return (f"{STATS['turns']} chamadas ao modelo em {STATS['seconds']:.0f}s · tokens: entrada="
            f"{STATS['in']:,} (cache={STATS['cache']:,}) saída={STATS['out']:,}").replace(",", ".") + cost


STREAM_RETRY_WAITS = (3, 10)  # segundos de espera antes da 2ª e da 3ª tentativa de uma volta
EXTRA_NUDGE_TURNS = 2  # voltas a mais, depois do limite, só para o modelo entregar o resultado


def _nudge(final_tool: dict) -> str:
    return (f"Limite de consultas atingido. Não leia mais arquivos: entregue agora o resultado chamando "
            f"a ferramenta {final_tool['name']}, com o que você já verificou.")


def _request(client, cfg: Config, system: list[dict], messages: list, final_tool: dict, stage: str, turn: int):
    """Uma volta com streaming. O Opus 5.5 não aceita tool_choice forçado: a entrega é pedida por instrução."""
    with client.beta.messages.stream(
        model=cfg.model,
        max_tokens=cfg.max_output_tokens,
        system=system,
        tools=[*tools.TOOLS, final_tool],
        tool_choice={"type": "auto"},
        messages=messages,
        cache_control={"type": "ephemeral"},  # também guarda em cache o histórico, que cresce a cada volta
        thinking={"type": "adaptive", "display": "summarized"},
        output_config={"effort": cfg.effort},
        betas=[FALLBACK_BETA],
        fallbacks="default",  # se o modelo recusar o trecho, a API repete em outro modelo na mesma chamada
    ) as stream:
        for event in stream:
            if event.type == "thinking":
                ui.thought(event.thinking)
            elif event.type == "text":
                ui.stream(event.text)
            elif event.type == "content_block_start":
                block = event.content_block
                if block.type == "thinking":
                    ui.wait_label(f"{stage} · volta {turn}: pensando")
                elif block.type == "tool_use":
                    what = ("escrevendo o resultado" if block.name == final_tool["name"]
                            else f"preparando {block.name}")
                    ui.wait_label(f"{stage} · volta {turn}: {what}")
            elif event.type == "input_json":
                ui.wait_progress(len(event.partial_json))
        return stream.get_final_message()


def run_agent(client, cfg: Config, repo: Path, head: str, stage: str,
              system: list[dict], user: str, final_tool: dict) -> dict:
    """Laço: o modelo consulta o código com as ferramentas de leitura até chamar `final_tool`.

    O histórico só cresce (nada é editado): o Opus 5.5 invalida o raciocínio anterior se o histórico mudar.
    """
    messages: list = [{"role": "user", "content": user}]
    for turn in range(1, cfg.max_turns + EXTRA_NUDGE_TURNS + 1):
        over_limit = turn >= cfg.max_turns
        t0 = time.monotonic()
        # O SDK repete sozinho o pedido inicial, mas não uma resposta que cai NO MEIO do streaming:
        # essa volta é refeita aqui (as mensagens anteriores, com os resultados das ferramentas, continuam as mesmas).
        for attempt, wait in enumerate((*STREAM_RETRY_WAITS, None), 1):
            ui.wait_start(f"{stage} · volta {turn}: aguardando o modelo")
            try:
                msg = _request(client, cfg, system, messages, final_tool, stage, turn)
                break
            except Exception as exc:
                if wait is None or not errors.is_transient(exc):
                    raise
                ui.wait_stop()
                ui.warn(f"{errors.explain(exc)[0]}; refazendo esta volta (tentativa {attempt + 1} de "
                        f"{len(STREAM_RETRY_WAITS) + 1}) em {wait}s")
                time.sleep(wait)
            finally:
                ui.wait_stop()
        took = time.monotonic() - t0
        _account(getattr(msg, "model", cfg.model) or cfg.model, msg.usage, took)
        ui.usage(f"{stage} · volta {turn}", msg.usage, took)

        if msg.stop_reason == "refusal":
            details = getattr(msg, "stop_details", None)
            raise errors.ModelRefusal(getattr(details, "category", None) or "não informada")
        if msg.stop_reason == "max_tokens":
            raise RuntimeError(f"Resposta cortada em max_tokens ({stage}); aumente MAX_OUTPUT_TOKENS ou reduza CHUNK_CHARS")

        calls = [b for b in msg.content if b.type == "tool_use"]
        final = next((b for b in calls if b.name == final_tool["name"]), None)
        if final:
            return final.input

        messages.append({"role": "assistant", "content": msg.content})
        if not calls:
            messages.append({"role": "user", "content": _nudge(final_tool) if over_limit else
                             f"Entregue agora o resultado pela ferramenta {final_tool['name']}."})
            continue

        if turn + 1 >= cfg.max_turns and not over_limit:
            ui.warn(f"chegando ao limite de {cfg.max_turns} voltas; pedindo a entrega do resultado")
        results = []
        for call in calls:
            if over_limit:  # depois do limite, nada de novas leituras: só a entrega
                out = f"erro: {_nudge(final_tool)}"
            else:
                ui.tool_call(call.name, call.input)
                out = tools.execute(cfg, repo, head, call.name, call.input)
                ui.tool_result(_summary(out))
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": out})
        if turn + 1 >= cfg.max_turns:
            results.append({"type": "text", "text": _nudge(final_tool)})
        messages.append({"role": "user", "content": results})

    raise RuntimeError(f"Etapa {stage} terminou sem entregar o resultado")


def _verify(client, cfg: Config, repo: Path, head: str, chunk: str, candidates: list[dict],
            kind: str = "diff", groups: frozenset[str] | None = None) -> list[dict]:
    """Segunda etapa da cadeia: tenta refutar cada achado antes de ele chegar ao relatório."""
    ui.step(f"Verificando {len(candidates)} achado(s)")
    numbered = json.dumps(
        [{"index": i, **{k: v for k, v in f.items()}} for i, f in enumerate(candidates)],
        ensure_ascii=False, indent=2,
    )
    tag, what = {"code": ("codigo", "o código"), "theme": ("inventario", "o inventário e o código")}.get(
        kind, ("diff", "o diff"))
    user = (f"Verifique os achados candidatos abaixo contra {what}.\n\n"
            f"<achados>\n{numbered}\n</achados>\n\n<{tag}>\n{chunk}\n</{tag}>")
    out = run_agent(client, cfg, repo, head, "verificação", _prompt(cfg, "verify", groups), user, REPORT_VERDICTS)
    verdicts = {v["index"]: v for v in out["verdicts"]}

    kept = []
    for i, f in enumerate(candidates):
        v = verdicts.get(i)
        if v is None:  # sem veredito: mantém, é o lado seguro
            kept.append(f)
        elif v["verdict"] == "refuted":
            ui.dropped(f, v["motivo"])
        else:
            if v["verdict"] == "uncertain":
                ui.info(f"? inconclusivo: {f['title']} — {v['motivo']}")
                f = {**f, "description": f"{f['description']} (Verificação inconclusiva: {v['motivo']})"}
            else:
                note = ""
                if v.get("severity") in SEVERITIES and v["severity"] != f["severity"]:
                    note = f" (severidade {f['severity']} → {v['severity']})"
                    f = {**f, "severity": v["severity"]}
                ui.info(f"✓ confirmado{note}: {f['title']} — {v['motivo']}")
            kept.append(f)
    return kept


def _hints_block(hints: list[str] | None) -> str:
    if not hints:
        return ""
    lines = "\n".join(f"- {h}" for h in hints)
    return ("\n\n<pistas>\nPistas dos scripts de verificação automática (determinísticos). Cada pista é um indício, "
            "não uma conclusão: confirme no código antes de reportar e, se não proceder, registre em `verificados` "
            f"por que foi descartada.\n{lines}\n</pistas>")


def _user_prompt(kind: str, head: str, chunk: str, label: str, i: int, n: int, hints: list[str] | None,
                 theme_instruction: str = "") -> str:
    if kind == "theme":
        return (f"Revisão transversal da branch main (commit {head[:7]}), tema {label!r}.\n{theme_instruction}\n"
                f"O inventário abaixo foi montado por script a partir do código; use as ferramentas para ler os "
                f"arquivos que importam antes de concluir.\n\n<inventario>\n{chunk}\n</inventario>"
                + _hints_block(hints))
    if kind == "code":
        return (f"Revise o código atual da branch main (commit {head[:7]}), unidade {label!r}. "
                f"Os arquivos vêm completos e com linhas numeradas.\n\n<codigo>\n{chunk}\n</codigo>"
                + _hints_block(hints))
    return (f"Revise este diff (parte {i} de {n}) da branch main, commit {head[:7]}.\n\n"
            f"<diff>\n{chunk}\n</diff>" + _hints_block(hints))


def review_chunks(cfg: Config, repo: Path, head: str, chunks: list[str], kind: str = "diff",
                  labels: list[str] | None = None,
                  groups: frozenset[str] | None = None,
                  hints: list[str] | None = None,
                  dual: bool = False,
                  theme_instruction: str = "") -> tuple[list[dict], list[str]]:
    """Retorna (achados, itens conferidos sem problema).

    kind: 'diff' (mudanças), 'code' (arquivos inteiros) ou 'theme' (revisão transversal sobre um inventário).
    dual: além do revisor do checklist, roda um revisor com olhar de atacante; a verificação junta e filtra os dois.
    """
    client = make_client(cfg)
    system = _prompt(cfg, "review", groups)
    findings: list[dict] = []
    verified: list[str] = []

    for i, chunk in enumerate(chunks, 1):
        label = labels[i - 1] if labels else f"parte {i}"
        if kind == "diff":
            ui.step(f"Revisando bloco {i}/{len(chunks)} ({len(chunk):,} caracteres de diff)".replace(",", "."))
        else:
            title = "Revisão transversal" if kind == "theme" else "Revisando o código"
            ui.step(f"{title}: {label} ({len(chunk):,} caracteres)".replace(",", "."))
        if hints:
            ui.info(f"{len(hints)} pista(s) dos scripts automáticos para conferir")
        user = _user_prompt(kind, head, chunk, label, i, len(chunks), hints, theme_instruction)

        out = run_agent(client, cfg, repo, head, "revisão", system, user, REPORT_FINDINGS)
        ui.analysis(out.get("resumo", ""))
        candidates = list(out.get("findings", []))
        verified.extend(out.get("verificados", []))

        if dual:
            ui.step(f"Segunda revisão (olhar de atacante): {label}")
            attack = run_agent(client, cfg, repo, head, "atacante", _prompt(cfg, "attack", groups), user, REPORT_FINDINGS)
            ui.analysis(attack.get("resumo", ""))
            extra = attack.get("findings", [])
            ui.info(f"atacante: {len(extra)} candidato(s) além dos {len(candidates)} do checklist")
            candidates += extra

        if cfg.verify and candidates:
            candidates = _verify(client, cfg, repo, head, chunk, candidates, kind, groups)
        for f in candidates:
            ui.finding(f)
        findings.extend(candidates)

    findings.sort(key=lambda f: SEVERITIES.index(f["severity"]))
    return findings, verified
