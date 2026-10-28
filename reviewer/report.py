import html
from collections import Counter

from .llm import SEVERITIES

EMOJI = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵", "info": "⚪"}
LABEL = {"critical": "Crítico", "high": "Alto", "medium": "Médio", "low": "Baixo", "info": "Info"}


def ref_label(base: str, head: str) -> str:
    """Modo diff: 'abc1234..def5678'. Modo código (sem base): 'main@def5678'."""
    return f"{base[:7]}..{head[:7]}" if base else f"main@{head[:7]}"


def _counts(findings: list[dict]) -> str:
    c = Counter(f["severity"] for f in findings)
    return " · ".join(f"{EMOJI[s]} {c[s]} {LABEL[s]}" for s in SEVERITIES if c[s])


def _loc(f: dict) -> str:
    return f"{f['file']}:{f['line']}" if f.get("line") else f["file"]


def telegram_summary(run_id: int, base: str, head: str, findings: list[dict], top: int = 8, note: str = "",
                     project: str = "projeto", ask_slack: bool = True) -> str:
    lines = [
        f"<b>Revisão #{run_id} — {html.escape(project)}</b>",
        f"<code>{ref_label(base, head)}</code>",
        "",
        _counts(findings),
        "",
    ]
    for f in findings[:top]:
        lines.append(
            f"{EMOJI[f['severity']]} <b>{html.escape(f['title'])}</b>\n"
            f"<code>{html.escape(_loc(f))}</code>"
        )
    if len(findings) > top:
        lines.append(f"\n… e mais {len(findings) - top} no arquivo anexo.")
    if note:
        lines.append(f"\n{html.escape(note)}")
    if ask_slack:
        lines.append("\nEnviar o resumo ao Slack?")
    return "\n".join(lines)[:4000]


def markdown_full(run_id: int, base: str, head: str, findings: list[dict], verified: list[str] | None = None,
                  project: str = "projeto") -> str:
    out = [f"# {'Simulação' if run_id == 0 else f'Revisão #{run_id}'} — {project}", "", f"Referência: `{ref_label(base, head)}`", "", _counts(findings), ""]
    for f in findings:
        rule = f" `{f['regra']}`" if f.get("regra") else ""
        out += [
            f"## {EMOJI[f['severity']]} [{LABEL[f['severity']]}]{rule} {f['title']}",
            f"`{_loc(f)}`",
            "",
            f["description"],
            "",
            f"**Sugestão:** {f['suggestion']}",
            "",
        ]
    if verified:
        out += ["## Conferido sem problemas", ""] + [f"- {v}" for v in verified] + [""]
    return "\n".join(out)


def _slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slack_message(run_id: int, base: str, head: str, findings: list[dict], include_details: bool,
                  project: str = "projeto") -> str:
    lines = [f"*Revisão #{run_id} — {_slack_escape(project)}* `{ref_label(base, head)}`", _counts(findings), ""]
    for f in findings:
        lines.append(f"{EMOJI[f['severity']]} *{_slack_escape(f['title'])}* — `{_slack_escape(_loc(f))}`")
        if include_details:
            lines.append(f"> {_slack_escape(f['description'])}")
    return "\n".join(lines)
