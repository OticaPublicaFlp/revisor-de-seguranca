"""Benchmark de recall: o agente encontra falhas REAIS que já foram corrigidas?

Cada caso (profiles/<perfil>/casos.json) aponta o commit anterior a uma correção de segurança e os arquivos onde a falha estava.
O agente revisa esse código antigo (somente leitura, pelos objetos do git) e o resultado é comparado com o esperado.

Ressalva: o checklist (prompts/rules.md) foi escrito a partir dos AGENTS.md que descrevem essas mesmas correções.
Com o checklist, o benchmark mede se o agente APLICA as regras; com --sem-regras, mede se ele acha a falha sozinho.
"""
import json
import time
import unicodedata
from datetime import datetime
from pathlib import Path

from . import errors, gitrepo, llm, prechecks, ui, units
from .config import ROOT, Config

ORDER = ["critical", "high", "medium", "low", "info"]


def _norm(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))


def match(case: dict, findings: list[dict]) -> tuple[str, dict | None]:
    """'achou' (arquivo + palavra-chave), 'parcial' (só palavra-chave) ou 'perdeu'. Casamento heurístico."""
    keys = [_norm(k) for k in case["match_any"]]
    best = None
    for f in findings:
        text = _norm(f"{f['title']} {f['description']}")
        if not any(k in text for k in keys):
            continue
        if _norm(case["match_file"]) in _norm(f["file"]):
            return "achou", f
        best = best or f
    return ("parcial", best) if best else ("perdeu", None)


def _unit(cfg: Config, repo, case: dict) -> units.Unit:
    sha, files = case["prefix_commit"], []
    for path in case["files"]:
        size = int(gitrepo._git(cfg, repo, "cat-file", "-s", f"{sha}:{path}").strip())
        blob = gitrepo._git(cfg, repo, "rev-parse", f"{sha}:{path}").strip()
        files.append((path, size, blob))
    return units.Unit(key=f"bench:{case['id']}", files=tuple(files), prio=0)


def run(cfg: Config, only: list[str] | None = None, limit: int | None = None, no_rules: bool = False,
        start: int = 1) -> Path:
    path = cfg.profile.cases_path
    if not path.is_file():
        raise SystemExit(f"O perfil '{cfg.profile.name}' não tem casos de benchmark ({path}). "
                         "Veja no README como montar um casos.json com falhas reais já corrigidas.")
    cases = json.loads(path.read_text(encoding="utf-8"))["cases"]
    if only:
        cases = [c for c in cases if c["id"] in only]
    cases = cases[max(start, 1) - 1:]
    if limit:
        cases = cases[:limit]
    ui.step(f"Benchmark: {len(cases)} caso(s) de falhas reais já corrigidas"
            + (" · SEM o checklist (mede generalização)" if no_rules else " · com o checklist"))
    ui.info(f"modelo {cfg.model} · esforço {cfg.effort} · revisão dupla {'sim' if cfg.dual_review != 'off' else 'não'}"
            f" · verificação {'sim' if cfg.verify else 'não'}")

    ui.wait_start("baixando o histórico do GitHub")
    try:
        repo, _head = gitrepo.sync(cfg)
    finally:
        ui.wait_stop()
    llm.reset_stats()

    results = []
    for n, case in enumerate(cases, 1):
        ui.step(f"[{n}/{len(cases)}] {case['id']}: {case['title']}")
        sha = case["prefix_commit"]
        t0 = time.monotonic()
        try:
            unit = _unit(cfg, repo, case)
            hints, _ctx = prechecks.run(cfg, repo, sha)
            groups = frozenset() if no_rules else frozenset(case["rule_groups"])
            found, _verified = llm.review_chunks(
                cfg, repo, sha, [units.render(cfg, repo, sha, unit)], kind="code", labels=[case["id"]],
                groups=groups, hints=prechecks.for_files(hints, unit.paths), dual=cfg.dual_review != "off")
            status, hit = match(case, found)
            error = None
        except Exception as exc:
            found, status, hit, error = [], "erro", None, errors.explain(exc)[0]
            ui.warn(f"caso não concluído: {error}")
        sev_ok = bool(hit) and ORDER.index(hit["severity"]) <= ORDER.index(case["severity_min"])
        mark = {"achou": "✔ ACHOU", "parcial": "~ PARCIAL", "perdeu": "✖ PERDEU", "erro": "! ERRO"}[status]
        ui.info(f"{mark} · esperado: {case['expected'][:140]}")
        if hit:
            ui.info(f"  achado correspondente: [{hit['severity']}] {hit['title']} ({hit['file']})"
                    + ("" if sev_ok else f" · severidade abaixo de {case['severity_min']}"))
        results.append({"id": case["id"], "status": status, "severity_ok": sev_ok, "error": error,
                        "seconds": round(time.monotonic() - t0), "match": hit, "findings": found,
                        "expected": case["expected"]})

    total = len(results)
    got = sum(r["status"] == "achou" for r in results)
    partial = sum(r["status"] == "parcial" for r in results)
    ui.step(f"Resultado: achou {got} de {total} · parcial {partial} · "
            f"perdeu {sum(r['status'] == 'perdeu' for r in results)} · erro {sum(r['status'] == 'erro' for r in results)}")
    for r in results:
        print(f"  {r['status']:<8} {'sev ok' if r['severity_ok'] else '      '}  {r['id']:<34} "
              f"{len(r['findings'])} achado(s)  {r['seconds']}s")
    ui.info(f"Custo: {llm.stats_summary()}")
    ui.info("O casamento é por palavra-chave: confira os 'parcial' e 'perdeu' no arquivo salvo antes de concluir.")

    out_dir = cfg.data_dir / "bench"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{datetime.now():%Y%m%d-%H%M%S}{'-sem-regras' if no_rules else ''}.json"
    path.write_text(json.dumps({"model": cfg.model, "effort": cfg.effort, "no_rules": no_rules,
                                "dual": cfg.dual_review != "off", "recall": f"{got}/{total}",
                                "cost": llm.stats_summary(), "results": results},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    ui.done(f"Recall: {got}/{total} ({100 * got // max(total, 1)}%) · detalhes em {path.relative_to(ROOT)}")
    return path
