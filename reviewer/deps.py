"""Dependências com vulnerabilidade conhecida, pela base pública OSV (osv.dev).

Envia ao OSV só nome e versão de pacotes (nada de código). Desligue com DEPS_CHECK=false.
O resultado vira achado direto (regra DEP-1), sem passar pelo modelo: é fato da base, não interpretação.
Falha de rede aqui nunca derruba a revisão: vira aviso.
"""
import json
import re
from urllib.parse import urlparse

import httpx

from . import gitrepo, ui
from .config import Config

OSV_BATCH = "https://api.osv.dev/v1/querybatch"
OSV_VULN = "https://api.osv.dev/v1/vulns/{}"
SEVERITY = {"CRITICAL": "critical", "HIGH": "high", "MODERATE": "medium", "MEDIUM": "medium", "LOW": "low"}
ORDER = ["critical", "high", "medium", "low", "info"]
ESM = re.compile(r"https://esm\.sh/(?:v\d+/)?(@[^/@]+/[^/@]+|[^/@]+)@(\d[^/?#]*)")


def _show(cfg: Config, repo, sha: str, path: str) -> str:
    return gitrepo._git(cfg, repo, "show", f"{sha}:{path}", ok=(0, 128))


def packages(cfg: Config, repo, sha: str) -> list[dict]:
    """[{name, version, dev, file}] do package-lock.json (front) e do deno.lock (Edge Functions, via esm.sh)."""
    found: dict[tuple, dict] = {}
    try:
        lock = json.loads(_show(cfg, repo, sha, "package-lock.json") or "{}")
    except json.JSONDecodeError:
        lock = {}
    for key, info in (lock.get("packages") or {}).items():
        if key.startswith("node_modules/") and info.get("version"):
            name = key.rsplit("node_modules/", 1)[1]
            prev = found.get((name, info["version"]))
            dev = bool(info.get("dev")) and (prev is None or prev["dev"])  # basta um uso de produção
            host = urlparse(info.get("resolved") or "").hostname
            found[(name, info["version"])] = {"name": name, "version": info["version"], "dev": dev,
                                              "file": "package-lock.json",
                                              "source": None if host in (None, "registry.npmjs.org") else host}
    deno = _show(cfg, repo, sha, "deno.lock")
    for name, version in set(ESM.findall(deno)):
        found.setdefault((name, version), {"name": name, "version": version, "dev": False, "file": "deno.lock",
                                           "source": None})
    return list(found.values())


def _severity(vuln: dict) -> str:
    raw = (vuln.get("database_specific") or {}).get("severity", "")
    return SEVERITY.get(str(raw).upper(), "medium")


def _fixed(vuln: dict, name: str) -> list[str]:
    fixed = set()
    for aff in vuln.get("affected", []):
        if (aff.get("package") or {}).get("name") != name:
            continue
        for rng in aff.get("ranges", []):
            fixed.update(ev["fixed"] for ev in rng.get("events", []) if "fixed" in ev)
    return sorted(fixed)


def findings(cfg: Config, repo, sha: str) -> list[dict]:
    pkgs = packages(cfg, repo, sha)
    if not pkgs:
        return []
    ui.info(f"consultando o OSV: {len(pkgs)} pacotes (só nome e versão)")
    try:
        with httpx.Client(timeout=30) as http:
            hits: list[tuple[dict, list[str]]] = []
            for start in range(0, len(pkgs), 500):
                chunk = pkgs[start:start + 500]
                body = {"queries": [{"package": {"name": p["name"], "ecosystem": "npm"}, "version": p["version"]}
                                    for p in chunk]}
                results = http.post(OSV_BATCH, json=body).raise_for_status().json().get("results", [])
                for pkg, res in zip(chunk, results):
                    ids = [v["id"] for v in res.get("vulns", [])]
                    if ids:
                        hits.append((pkg, ids))
            details: dict[str, dict] = {}
            for _pkg, ids in hits:
                for vid in ids:
                    if vid not in details:
                        details[vid] = http.get(OSV_VULN.format(vid)).raise_for_status().json()
    except (httpx.HTTPError, ValueError) as exc:
        ui.warn(f"checagem de dependências pulada (OSV indisponível: {type(exc).__name__})")
        return []

    out = []
    for pkg, ids in hits:
        vulns = [details[v] for v in ids]
        sev = min((_severity(v) for v in vulns), key=ORDER.index)
        if pkg["dev"]:  # ferramenta de build/teste: não vai para o navegador nem para o servidor
            sev = max(sev, "low", key=ORDER.index)
        fixed = sorted({f for v in vulns for f in _fixed(v, pkg["name"])})
        summaries = "; ".join(f"{v['id']}: {(v.get('summary') or 'sem resumo')[:120]}" for v in vulns[:5])
        scope = "dependência de desenvolvimento" if pkg["dev"] else "dependência de produção"
        caveat = ""
        if pkg.get("source"):
            # Há pacotes cujas versões corrigidas saem só pelo CDN do fornecedor, fora do npm. O OSV só conhece o npm
            # e marca todas as versões como afetadas, mesmo as do fornecedor que já trazem a correção.
            sev = "info"
            caveat = (f" Atenção: instalado de fora do registro npm ({pkg['source']}). O OSV só conhece as versões "
                      f"publicadas no npm e pode marcar como afetada uma versão do fornecedor que já tem a correção; "
                      f"confirme no changelog do fornecedor.")
        out.append({
            "regra": "DEP-1",
            "severity": sev,
            "file": pkg["file"],
            "line": None,
            "title": f"{pkg['name']}@{pkg['version']} tem {len(vulns)} vulnerabilidade(s) conhecida(s)",
            "description": f"{scope.capitalize()} com avisos públicos no OSV: {summaries}.{caveat}",
            "suggestion": (f"Atualizar para uma versão corrigida ({', '.join(fixed[:4])})." if fixed
                           else "Sem versão corrigida publicada: avaliar substituir o pacote ou mitigar o uso."),
        })
    out.sort(key=lambda f: ORDER.index(f["severity"]))
    return out
