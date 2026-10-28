"""Revisões transversais: um inventário montado por script dá ao revisor a visão do sistema inteiro.

Muitas falhas atravessam arquivos (a tela chama uma RPC, a RPC confia numa policy de outra migration). A revisão por
unidade não vê isso; cada tema aqui junta, numa tabela, tudo que importa para uma pergunta de segurança.
"""
import re

from . import units
from .prechecks import CLIENT_ROLES

THEMES = {
    "entradas": ("Pontos de entrada das Edge Functions", frozenset({"EF", "DB", "CX"}),
                 "Para cada Edge Function, decida quem consegue chamá-la (internet, qualquer logado, papel X, cron) e "
                 "se o código garante isso. Procure funções cuja proteção depende de algo que o inventário não mostra."),
    "acesso-publico": ("O que alguém sem login alcança", frozenset({"EF", "SQL", "CX"}),
                       "Liste o que é alcançável sem login (funções com verify_jwt=false, objetos SQL liberados a "
                       "anon/public, páginas públicas) e verifique se cada um devolve ou altera só o mínimo necessário."),
    "dados-sensiveis": ("Toda saída de dados sensíveis", frozenset({"EF", "DB", "SQL", "FE"}),
                        "Siga cada caminho que lê os dados sensíveis listados e confirme: papel conferido no servidor, "
                        "filtro pelo cliente (tenant) do chamador, limite de linhas e registro de auditoria quando exporta."),
    "funcoes-privilegiadas": ("Funções SQL privilegiadas", frozenset({"SQL", "DB"}),
                              "Cada função SECURITY DEFINER executável pelo app ignora a RLS. Confirme que ela filtra o "
                              "cliente (tenant) do chamador e confere papel antes de escrever; leia a definição final de cada uma."),
}


def _yn(v) -> str:
    return "sim" if v else "não"


def _edge_table(edge: list[dict], only=None) -> str:
    rows = ["| função | verify_jwt | autenticação no código | service role | filtro cliente | papel | auditoria | fetch externo |",
            "|---|---|---|---|---|---|---|---|"]
    for r in edge:
        if only and not only(r):
            continue
        jwt = {True: "true", False: "FALSE", None: "(sem bloco)"}[r["verify_jwt"]]
        fetch = ("validado" if r["safe_url"] else "NÃO validado") if r["external_fetch"] else "—"
        rows.append(f"| {r['name']} | {jwt} | {', '.join(r['auth']) or 'NENHUMA'} | {_yn(r['service_role'])} | "
                    f"{_yn(r['tenant_filter'])} | {', '.join(r['role_guard']) or '—'} | {', '.join(r['audit']) or '—'} | {fetch} |")
    return "\n".join(rows)


def _definers(sql: dict, p, only=None) -> list[str]:
    rows = ["| função | arquivo:linha | executável por | confere papel | filtro cliente | escreve | search_path |",
            "|---|---|---|---|---|---|---|"]
    for name, f in sorted(sql["functions"].items()):
        low = f["text"].lower()
        callers = set(f["exec"]) & CLIENT_ROLES
        if "security definer" not in low or not callers or re.search(r"returns\s+(setof\s+)?trigger", low):
            continue
        if only and not only(name, f, low):
            continue
        writes = bool(re.search(r"\b(insert\s+into|update\s+[\w.\"]+\s+set|delete\s+from)\b", low))
        rows.append(f"| {name} | {f['file']}:{f['line']} | {', '.join(sorted(callers))} | "
                    f"{_yn(any(rc.lower() in low for rc in p.role_checks))} | {_yn(p.tenant_filter.search(low))} | {_yn(writes)} | "
                    f"{_yn('search_path' in low)} |")
    return rows


def _inventory(theme: str, ctx: dict, src_files: list[str], p) -> str:
    edge, sql = ctx["edge"], ctx["sql"]
    if theme == "entradas":
        return "## Edge Functions\n" + _edge_table(edge)
    if theme == "acesso-publico":
        public_sql = _definers(sql, p, lambda n, f, low: {"public", "anon"} & set(f["exec"]))
        policies = [f"- policy '{k[0]}' em {k[1]} → {', '.join(pol['roles'])} ({pol['file']}:{pol['line']})"
                    for k, pol in sql["policies"].items() if any(r.startswith(("public", "anon")) for r in pol["roles"])]
        grants = [f"- GRANT {g['privs']} ON {k[0]} TO {k[1]} ({g['file']}:{g['line']})" for k, g in sql["grants"].items()]
        pages = [f for f in src_files if re.search(r"(?i)public|survey|pesquisa|status|landing", f)]
        return "\n\n".join([
            "## Edge Functions com verify_jwt=false (sem JWT na borda)\n"
            + _edge_table(edge, lambda r: r["verify_jwt"] is False),
            "## Funções SQL executáveis por anon/public\n" + ("\n".join(public_sql) if len(public_sql) > 2 else "nenhuma"),
            "## Policies explicitamente para public/anon\n" + ("\n".join(policies) or "nenhuma"),
            "## GRANTs para anon/public no estado final\n" + ("\n".join(grants) or "nenhum"),
            "## Arquivos de front-end com cara de página pública\n" + ("\n".join(f"- {f}" for f in pages) or "nenhum"),
        ])
    if theme == "dados-sensiveis":
        tables = ", ".join(p.sensitive_tables) or "(nenhuma tabela sensível configurada no perfil)"
        touches = lambda n, f, low: any(t.lower() in low for t in p.sensitive_tables)  # noqa: E731
        exports = [f for f in src_files if re.search(r"(?i)export|report|relatorio|xlsx|pdf", f)]
        return "\n\n".join([
            f"Tabelas sensíveis ({p.sensitive_label}): {tables}",
            "## Edge Functions que leem as tabelas sensíveis\n" + _edge_table(edge, lambda r: r["reads_sensitive"]),
            "## Funções SQL privilegiadas que tocam as tabelas sensíveis\n" + "\n".join(_definers(sql, p, touches)),
            "## Arquivos de front-end ligados a exportação/relatório\n" + "\n".join(f"- {f}" for f in exports[:60]),
        ])
    return "## Funções SECURITY DEFINER executáveis pelo app\n" + "\n".join(_definers(sql, p))


def build(ctx: dict, src_files: list[str], p) -> list[units.Unit]:
    out = []
    for key, (title, groups, instruction) in THEMES.items():
        inventory = f"# {title}\n\n{_inventory(key, ctx, src_files, p)}"
        out.append(units.Unit(key=f"tema:{key}", files=(), prio=0, order=-1, kind="theme",
                              inventory=inventory, instruction=instruction, groups=groups))
    return out
