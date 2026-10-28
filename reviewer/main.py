import argparse
import asyncio
import html
import json
import logging
import sys
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from croniter import croniter

from . import deps, errors, gitrepo, llm, prechecks, report, slack, themes, ui, units
from .config import ROOT, Config, load_config
from .db import Store, now_iso
from .telegram import Telegram

log = logging.getLogger("reviewer")


# ---------------------------------------------------------------- pipeline

def run_review(cfg: Config, store: Store, tg: Telegram | None, dry_run: bool = False) -> None:
    store.set("last_run_at", now_iso())
    llm.reset_stats()
    ui.step(f"Sincronizando {cfg.repo_branch} do repositório remoto")
    ui.wait_start("baixando o código do GitHub")
    try:
        repo, head = gitrepo.sync(cfg)
    finally:
        ui.wait_stop()
    if cfg.mode == "diff":
        _review_diff(cfg, store, tg, repo, head, dry_run)
    else:
        _review_code(cfg, store, tg, repo, head, dry_run)


def _deliver(cfg: Config, store: Store, tg: Telegram | None, base: str, head: str, new: list[dict],
             verified: list[str], note: str, dry_run: bool) -> int | None:
    """Grava a execução e envia ao Telegram. Em dry-run só imprime. Retorna o id da execução."""
    ref = report.ref_label(base, head)
    verified = list(dict.fromkeys(verified))  # sem itens repetidos, mantendo a ordem
    ui.info(f"Resumo: {llm.stats_summary()}")
    if dry_run:
        ui.done(f"Fim da simulação: {len(new)} achado(s) novo(s) (nada foi enviado nem gravado)")
        print(report.markdown_full(0, base, head, new, verified, project=cfg.profile.project) if new else "Sem achados novos.")
        return None

    run_id = store.create_run(base, head, new)
    ui.step("Enviando relatório ao Telegram")
    if new:
        tg.send_document(f"revisao-{run_id}.md", report.markdown_full(run_id, base, head, new, verified, project=cfg.profile.project))
        slack_on = bool(cfg.slack_token and cfg.slack_channel)
        tg.send_summary(report.telegram_summary(run_id, base, head, new, note=note, project=cfg.profile.project,
                                                ask_slack=slack_on), run_id, slack_enabled=slack_on)
    else:
        tg.send_text(f"✅ Revisão #{run_id}: sem achados novos em <code>{ref}</code>."
                     + (f"\n{html.escape(note)}" if note else ""))
    return run_id


def _review_diff(cfg: Config, store: Store, tg: Telegram | None, repo, head: str, dry_run: bool) -> None:
    last = store.get("last_sha")
    if last == head:
        ui.done(f"Sem commits novos desde {head[:7]}")
        if tg and not dry_run:
            tg.send_text(f"✅ Revisão: sem commits novos na <code>{cfg.repo_branch}</code> (<code>{head[:7]}</code>).")
        return

    base = gitrepo.resolve_base(cfg, repo, last, head)
    files = gitrepo.diff_by_file(cfg, repo, base, head)
    chunks = gitrepo.chunk_diffs(files, cfg.chunk_chars)
    partial = len(chunks) > cfg.max_chunks
    chunks = chunks[: cfg.max_chunks]
    ui.info(f"{base[:7]}..{head[:7]}: {len(files)} arquivo(s) no diff, {len(chunks)} bloco(s)"
            + (" — PARCIAL: excede MAX_CHUNKS" if partial else ""))
    for name, _ in files[:15]:
        ui.info(f"  · {name}")
    if len(files) > 15:
        ui.info(f"  · … e mais {len(files) - 15}")

    findings, verified = llm.review_chunks(cfg, repo, head, chunks) if chunks else ([], [])
    new = store.filter_new(findings)
    if len(new) < len(findings):
        ui.info(f"{len(findings) - len(new)} achado(s) já reportado(s) antes foram omitidos")

    note = "⚠️ Revisão parcial: o diff excedeu MAX_CHUNKS." if partial else ""
    run_id = _deliver(cfg, store, tg, base, head, new, verified, note, dry_run)
    if run_id is None:
        return
    # Só avança o marcador depois de entregar o relatório; se algo acima falhar, a próxima execução repete.
    store.set("last_sha", head)
    ui.done(f"Revisão #{run_id} concluída: {len(new)} achado(s) novo(s) enviados ao Telegram")


def _plan_inputs(cfg: Config, repo, head: str) -> tuple[list, list]:
    """Unidades de código + temas transversais, e as pistas dos scripts (sem custo de modelo)."""
    ui.info("rodando as verificações automáticas (scripts, sem custo)")
    hints, ctx = prechecks.run(cfg, repo, head)
    all_units = units.build(cfg, repo, head) + themes.build(ctx, units.src_files(cfg, repo, head), cfg.profile)
    ui.info(f"{len(hints)} pista(s) encontradas pelos scripts")
    return all_units, hints


def _dual(cfg: Config, u) -> bool:
    """Revisão dupla (checklist + atacante) onde o risco é maior: Edge Functions, migrations e temas."""
    return cfg.dual_review == "all" or (cfg.dual_review == "critical" and u.prio <= 1)


def _review_code(cfg: Config, store: Store, tg: Telegram | None, repo, head: str, dry_run: bool) -> None:
    all_units, hints = _plan_inputs(cfg, repo, head)
    reviewed = store.get_units()
    plan = units.select(reviewed, all_units, cfg)
    fresh = {u.key for u in all_units if units.status(reviewed, u) == "em dia"}
    ui.info(f"{len(all_units)} unidade(s) em {cfg.repo_branch}@{head[:7]} (incluindo {len(themes.THEMES)} temas); "
            f"{len(fresh)} em dia; {len(plan)} para revisar agora (MAX_UNITS={cfg.max_units})")
    for u, reason in plan:
        ui.info(f"  · {u.key} — {reason}" + (" · revisão dupla" if _dual(cfg, u) else ""))

    dep_findings = []
    if cfg.deps_check:
        ui.step("Dependências com vulnerabilidade conhecida (OSV)")
        dep_findings = deps.findings(cfg, repo, head)
        for f in dep_findings:
            ui.finding(f)
        if not dep_findings:
            ui.info("nenhuma vulnerabilidade conhecida nas dependências")

    if not plan and not store.filter_new(dep_findings):
        ui.done("Todas as unidades estão em dia; nada a revisar")
        if tg and not dry_run:
            tg.send_text(f"✅ Revisão: as {len(all_units)} unidades de código estão em dia "
                         f"(sem alteração desde a última revisão).")
        return

    findings, verified, done, failure = list(dep_findings), [], [], None
    for u, _reason in plan:
        groups = units.rule_groups(u.key)
        unit_hints = (prechecks.for_rules(hints, groups) if u.kind == "theme"
                      else prechecks.for_files(hints, u.paths))
        label = u.key if u.kind == "theme" else f"{u.key} — {len(u.files)} arquivo(s)"
        try:
            f, v = llm.review_chunks(cfg, repo, head, [units.render(cfg, repo, head, u)], kind=u.kind,
                                     labels=[label], groups=groups, hints=unit_hints, dual=_dual(cfg, u),
                                     theme_instruction=u.instruction)
        except Exception as exc:  # uma unidade com falha não pode jogar fora as já revisadas
            log.debug("Falha ao revisar %s", u.key, exc_info=True)
            failure = (u.key, exc)
            break
        findings += f
        verified += v
        done.append(u)
    if failure and not done:
        raise failure[1]  # nada foi revisado: o erro explicado aparece uma vez, no final
    if failure:  # revisão parcial: avisa o que falhou e segue entregando o que foi revisado
        title, _detail, hint = errors.explain(failure[1])
        ui.warn(f"falha em {failure[0]}: {title}")
        ui.info(f"→ {hint}")

    new = store.filter_new(findings)
    if len(new) < len(findings):
        ui.info(f"{len(findings) - len(new)} achado(s) já reportado(s) antes foram omitidos")

    covered = len(fresh | {u.key for u in done})
    note = (f"Cobertura: {covered}/{len(all_units)} unidades. Revisadas agora: "
            f"{', '.join(u.key for u in done) or 'nenhuma (só dependências)'}.")
    if failure:
        note += f"\n⚠️ Falhou em {failure[0]} ({errors.explain(failure[1])[0]}); será tentada na próxima execução."
    run_id = _deliver(cfg, store, tg, "", head, new, verified, note, dry_run)
    if run_id is None:
        return
    for u in done:  # só marca como revisada depois de o relatório ter sido entregue
        store.mark_unit(u.key, u.fingerprint)
    ui.done(f"Revisão #{run_id} concluída: {len(done)} unidade(s), {len(new)} achado(s) novo(s) enviados ao Telegram")


def _sync(cfg: Config):
    ui.step(f"Sincronizando {cfg.repo_branch}")
    ui.wait_start("baixando o código do GitHub")
    try:
        return gitrepo.sync(cfg)
    finally:
        ui.wait_stop()


def show_units(cfg: Config, store: Store) -> None:
    """Lista as unidades (código e temas) e o que seria revisado na próxima execução (sem usar o modelo)."""
    repo, head = _sync(cfg)
    all_units, _hints = _plan_inputs(cfg, repo, head)
    reviewed = store.get_units()
    plan = {u.key: reason for u, reason in units.select(reviewed, all_units, cfg)}
    ui.step(f"{len(all_units)} unidades em {cfg.repo_branch}@{head[:7]} (MAX_UNITS={cfg.max_units})")
    for u in sorted(all_units, key=lambda x: (x.prio, x.order, x.key)):
        mark = "▶ próxima" if u.key in plan else "         "
        size = "tema" if u.kind == "theme" else f"{len(u.files):>3} arq"
        dual = " · dupla" if _dual(cfg, u) else ""
        print(f"  {mark}  {units.status(reviewed, u):<15} {size:>7} {u.chars:>8,} car  {u.key}{dual}".replace(",", "."))


def show_hints(cfg: Config) -> None:
    """Pistas dos scripts e dependências vulneráveis, sem usar o modelo."""
    repo, head = _sync(cfg)
    hints, _ctx = prechecks.run(cfg, repo, head)
    ui.step(f"{len(hints)} pista(s) dos scripts em {cfg.repo_branch}@{head[:7]}")
    for rule in sorted({h.rule for h in hints}):
        print(f"\n  {rule}")
        for h in (h for h in hints if h.rule == rule):
            print(f"    · {h.text().split('] ', 1)[1]}")
    if cfg.deps_check:
        ui.step("Dependências com vulnerabilidade conhecida (OSV)")
        found = deps.findings(cfg, repo, head)
        for f in found:
            ui.finding(f)
        if not found:
            ui.info("nenhuma vulnerabilidade conhecida")
    print("\nPistas são indícios para o revisor confirmar, não achados confirmados.")


def handle_callback(cfg: Config, store: Store, tg: Telegram, cq: dict) -> None:
    cid = cq["id"]
    # O botão tem de estar no chat configurado (só membros do grupo o veem). Se houver lista de
    # aprovadores, quem clicou também precisa estar nela; lista vazia = qualquer membro do grupo.
    if cq.get("message", {}).get("chat", {}).get("id") != cfg.telegram_chat_id or \
            (cfg.telegram_approver_ids and cq.get("from", {}).get("id") not in cfg.telegram_approver_ids):
        log.warning("Callback de remetente não autorizado: %s", cq.get("from", {}).get("id"))
        tg.answer_callback(cid, "Não autorizado.")
        return

    action, _, raw_id = cq.get("data", "").partition(":")
    if action not in ("slack", "drop") or not raw_id.isdigit():
        tg.answer_callback(cid, "")
        return
    run_id, message_id = int(raw_id), cq["message"]["message_id"]
    run = store.get_run(run_id)
    if run is None:
        tg.answer_callback(cid, "Revisão não encontrada.")
        return

    if action == "drop":
        if store.transition(run_id, "dropped"):
            tg.clear_keyboard(message_id, "❌ Descartado")
            tg.answer_callback(cid, "Descartado.")
        else:
            tg.answer_callback(cid, "Já decidido.")
        return

    if not store.transition(run_id, "sent"):
        tg.answer_callback(cid, "Já decidido.")
        return
    try:
        findings = json.loads(run["findings_json"])
        slack.post_message(cfg, report.slack_message(run_id, run["base_sha"], run["head_sha"],
                                                     findings, cfg.slack_include_details, project=cfg.profile.project))
    except Exception as exc:
        store.revert_to_pending(run_id)  # permite tentar de novo pelo mesmo botão
        log.exception("Falha ao enviar ao Slack")
        tg.answer_callback(cid, f"Falha no Slack: {exc}"[:190])
        return
    tg.clear_keyboard(message_id, "✅ Enviado ao Slack")
    tg.answer_callback(cid, "Enviado ao Slack.")


# ---------------------------------------------------------------- daemon

async def scheduler_loop(cfg: Config, store: Store, tg: Telegram, lock: asyncio.Lock) -> None:
    tz = ZoneInfo(cfg.tz)

    async def fire() -> None:
        async with lock:
            try:
                await asyncio.to_thread(run_review, cfg, store, tg)
            except Exception as exc:
                title, detail, hint = errors.explain(exc)
                ui.error(title, detail, hint)
                _save_error_log(exc)
                msg = f"⚠️ <b>Revisão falhou:</b> {html.escape(title)}"
                if detail:
                    msg += f"\n{html.escape(detail)}"
                msg += f"\n→ {html.escape(hint)}"
                try:
                    await asyncio.to_thread(tg.send_text, msg)
                except Exception as notify_exc:
                    ui.warn(f"não consegui avisar o erro no Telegram: {errors.explain(notify_exc)[0]}")

    # Catch-up: o WSL pode ter ficado desligado no horário agendado.
    now = datetime.now(tz)
    prev = croniter(cfg.cron_expr, now).get_prev(datetime)
    last = store.get("last_run_at")
    if last is None or datetime.fromisoformat(last) < prev:
        ui.info(f"Execução perdida (prevista para {prev:%d/%m %H:%M}); rodando agora")
        await fire()

    nxt = croniter(cfg.cron_expr, datetime.now(tz)).get_next(datetime)
    ui.step(f"Daemon ativo. Próxima revisão: {nxt:%d/%m/%Y %H:%M} ({cfg.tz}). Ctrl+C para sair.")
    while True:
        # Dorme em passos curtos: sobrevive a suspensão do WSL e a mudanças no relógio.
        await asyncio.sleep(30)
        if datetime.now(tz) >= nxt:
            await fire()
            nxt = croniter(cfg.cron_expr, datetime.now(tz)).get_next(datetime)
            ui.step(f"Aguardando. Próxima revisão: {nxt:%d/%m/%Y %H:%M} ({cfg.tz})")


async def telegram_loop(cfg: Config, store: Store, tg: Telegram, lock: asyncio.Lock) -> None:
    backoff = 5
    while True:
        try:
            offset = int(store.get("tg_offset") or 0)
            updates = await asyncio.to_thread(tg.get_updates, offset)
            backoff = 5
            for upd in updates:
                store.set("tg_offset", str(upd["update_id"] + 1))
                cq = upd.get("callback_query")
                if cq:
                    try:
                        await asyncio.to_thread(handle_callback, cfg, store, tg, cq)
                    except Exception:
                        log.exception("Erro tratando callback")
        except Exception:
            log.exception("Erro no polling do Telegram; nova tentativa em %ss", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


async def daemon(cfg: Config) -> None:
    store = Store(cfg.data_dir / "state.db")
    tg = Telegram(cfg)
    lock = asyncio.Lock()
    await asyncio.gather(scheduler_loop(cfg, store, tg, lock), telegram_loop(cfg, store, tg, lock))


# ---------------------------------------------------------------- CLI

def _save_error_log(exc: BaseException) -> Path:
    """Grava o traceback completo (com segredos ocultados) para diagnóstico."""
    path = ROOT / "data" / "last-error.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    trace = "".join(traceback.format_exception(exc))
    path.write_text(f"{now_iso()}\n{errors.redact(trace)}", encoding="utf-8")
    return path


def _fail(exc: BaseException, debug: bool) -> None:
    title, detail, hint = errors.explain(exc)
    ui.error(title, detail, hint)
    path = _save_error_log(exc)
    if debug:
        sys.stderr.write("\n" + errors.redact("".join(traceback.format_exception(exc))))
    else:
        sys.stderr.write(f"  Detalhe técnico: {path.relative_to(ROOT)} (ou rode com --debug)\n")
    sys.exit(1)


def _banner(cfg: Config, what: str) -> None:
    if cfg.mode == "code":
        scope = f"modo código, até {cfg.max_units} unidade(s) por execução"
    else:
        scope = f"modo diff, até {cfg.max_chunks} bloco(s)"
    ui.step(f"Revisor de segurança · {cfg.profile.project} (perfil {cfg.profile.name}): {what}")
    ui.info(f"{scope} · modelo {cfg.model} · verificação {'ligada' if cfg.verify else 'desligada'}"
            f" · máx. {cfg.max_turns} voltas por etapa")


def _dispatch(args) -> None:
    if args.cmd == "configurar":
        from .wizard import run_wizard
        run_wizard()
        return

    if args.cmd == "verificar":
        from .wizard import run_doctor
        if not run_doctor():
            sys.exit(1)
        return

    if args.cmd == "telegram-ids":
        from dotenv import load_dotenv
        import os
        from .telegram import discover
        load_dotenv(ROOT / ".env")
        discover(os.environ.get("TELEGRAM_BOT_TOKEN", "") or sys.exit("TELEGRAM_BOT_TOKEN ausente no .env"))
        return

    if args.cmd == "run":
        cfg = load_config()
        _banner(cfg, "daemon (agenda revisões e escuta os botões do Telegram)")
        asyncio.run(daemon(cfg))
        return

    if args.cmd == "units":
        cfg = load_config(need_telegram=False)
        show_units(cfg, Store(cfg.data_dir / "state.db"))
        return

    if args.cmd == "pistas":
        show_hints(load_config(need_telegram=False))
        return

    if args.cmd == "bench":
        from . import bench
        bench.run(load_config(need_telegram=False), only=args.caso, limit=args.limite, no_rules=args.sem_regras,
                  start=args.inicio)
        return

    cfg = load_config(need_telegram=not args.dry_run)
    _banner(cfg, "simulação (nada será enviado nem gravado)" if args.dry_run else "execução única")
    store = Store(cfg.data_dir / "state.db")
    run_review(cfg, store, None if args.dry_run else Telegram(cfg), dry_run=args.dry_run)


def main() -> None:
    parser = argparse.ArgumentParser(prog="revisor", description="Revisor de segurança de código com IA (somente leitura).")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="Sem o acompanhamento ao vivo no terminal (erros continuam aparecendo)")
    parser.add_argument("--debug", action="store_true", help="Mostra o detalhe técnico dos erros")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("configurar", help="Assistente de configuração (pergunta tudo e valida cada chave)")
    sub.add_parser("verificar", help="Confere a configuração e as conexões, sem gastar créditos de IA")
    sub.add_parser("run", help="Daemon: agenda revisões e escuta os botões do Telegram")
    sub.add_parser("units", help="Lista as unidades de código e quais seriam revisadas agora (sem usar o modelo)")
    sub.add_parser("pistas", help="Mostra as pistas dos scripts e as dependências vulneráveis (sem usar o modelo)")
    bench_p = sub.add_parser("bench", help="Mede o recall em falhas reais já corrigidas (USA o modelo: tem custo)")
    bench_p.add_argument("--caso", action="append", help="Roda só este caso (pode repetir)")
    bench_p.add_argument("--limite", type=int, help="Roda só os N primeiros casos")
    bench_p.add_argument("--inicio", type=int, default=1, help="Começa a partir do caso N (1 = primeiro)")
    bench_p.add_argument("--sem-regras", action="store_true",
                         help="Sem o checklist: mede se o agente acha a falha sozinho (generalização)")
    sub.add_parser("telegram-ids", help="Mostra o ID do grupo e das pessoas que o bot já viu")
    once = sub.add_parser("once", help="Executa uma revisão agora e sai")
    once.add_argument("--dry-run", action="store_true",
                      help="Imprime o relatório; não usa Telegram nem grava estado")
    args = parser.parse_args()

    ui.enabled = not args.quiet
    # O acompanhamento é feito pelo ui; os logs só mostram avisos e erros (e nunca as URLs com token)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("reviewer").setLevel(logging.DEBUG if args.debug else logging.WARNING)
    for noisy in ("httpx", "httpx2", "httpcore", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    try:
        _dispatch(args)
    except KeyboardInterrupt:
        ui.wait_stop()
        sys.stderr.write("\nInterrompido (Ctrl+C). Nada foi enviado ao grupo além do que já tinha sido mostrado.\n")
        sys.exit(130)
    except Exception as exc:
        _fail(exc, args.debug)


if __name__ == "__main__":
    main()
