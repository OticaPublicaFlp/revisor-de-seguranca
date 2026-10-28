import dataclasses
import re
from unittest import mock
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from reviewer import gitrepo, llm, tools, ui
from reviewer.config import Config
from test_pipeline import commit, git

ui.enabled = False


TEST_PROFILE = str(Path(__file__).resolve().parent / "fixtures" / "perfil")


def make_cfg(tmp: Path, origin: Path, **over) -> Config:
    base = Config(
        repo_url=str(origin), repo_branch="main", repo_token="", cron_expr="0 8 * * *", tz="UTC",
        initial_lookback=20, anthropic_api_key="x", model="m", max_chunks=6, chunk_chars=60000,
        max_output_tokens=100, max_turns=4, verify=True, telegram_token="t", telegram_chat_id=1, telegram_approver_ids=frozenset({1}),
        slack_token="", slack_channel="", slack_include_details=False, data_dir=tmp / "data", mode="diff",
        deps_check=False,  # sem rede nos testes
        profile_name=TEST_PROFILE,
    )
    base.data_dir.mkdir(exist_ok=True)
    return dataclasses.replace(base, **over)


class ToolsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        origin = self.tmp / "origin"
        origin.mkdir()
        git(origin, "init", "-q", "-b", "main")
        commit(origin, {
            "supabase/functions/foo/index.ts": "import { x } from '../_shared/auth.ts'\nconst a = 1\n",
            ".github/workflows/ci.yml": "name: ci\n",
            ".env": "SECRET=abc123\n",
            "src/key.pem": "-----BEGIN-----\n",
        }, "c1")
        self.cfg = make_cfg(self.tmp, origin)
        self.repo, self.head = gitrepo.sync(self.cfg)

    def run_tool(self, name, **args):
        return tools.execute(self.cfg, self.repo, self.head, name, args)

    def test_read_file_numbers_lines(self):
        out = self.run_tool("read_file", path="supabase/functions/foo/index.ts")
        self.assertIn("    1  import { x }", out)

    def test_dot_directories_are_preserved(self):
        self.assertIn("name: ci", self.run_tool("read_file", path=".github/workflows/ci.yml"))
        self.assertIn("name: ci", self.run_tool("read_file", path="./.github/workflows/ci.yml"))

    def test_secret_files_are_blocked(self):
        self.assertIn("bloqueado", self.run_tool("read_file", path=".env"))
        self.assertIn("bloqueado", self.run_tool("read_file", path="src/key.pem"))
        self.assertNotIn("abc123", self.run_tool("search_repo", pattern="SECRET"))
        self.assertNotIn(".env", self.run_tool("list_dir", path=""))

    def test_path_escape_is_rejected(self):
        for bad in ("../etc/passwd", "/etc/passwd", "-x", "a/../../b", "~/x"):
            self.assertIn("inválido", self.run_tool("read_file", path=bad), bad)

    def test_search_and_list(self):
        self.assertIn("supabase/functions/foo/index.ts:1:", self.run_tool("search_repo", pattern="_shared/auth"))
        self.assertEqual(self.run_tool("search_repo", pattern="naoexiste"), "nenhuma ocorrência")
        self.assertIn("index.ts", self.run_tool("list_dir", path="supabase/functions/foo"))

    def test_search_pattern_cannot_inject_options(self):
        # padrão que parece flag é só texto de busca (vai depois de -e)
        self.assertEqual(self.run_tool("search_repo", pattern="--help"), "nenhuma ocorrência")


class FakeClient:
    """Cliente Anthropic simulado: devolve respostas pré-definidas, uma por volta."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.messages = self
        self.beta = SimpleNamespace(messages=self)
        self.calls = []  # parâmetros de cada chamada, para conferir o que vai para a API

    @contextmanager
    def stream(self, **kwargs):
        self.calls.append(kwargs)
        content = self.turns.pop(0)
        failure = content if isinstance(content, Exception) else None
        msg = SimpleNamespace(content=content, stop_reason="tool_use",
                              usage=SimpleNamespace(input_tokens=1, output_tokens=1,
                                                    cache_read_input_tokens=0))
        class _Stream:
            def __iter__(self):
                if failure:
                    raise failure  # simula a conexão caindo no meio da resposta
                return iter(())  # sem eventos de streaming: o resultado vem de get_final_message

            @staticmethod
            def get_final_message():
                return msg
        yield _Stream()


def call(name, tool_id="t1", **inp):
    return SimpleNamespace(type="tool_use", name=name, id=tool_id, input=inp)


class AgentLoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        origin = self.tmp / "origin"
        origin.mkdir()
        git(origin, "init", "-q", "-b", "main")
        commit(origin, {"a.ts": "const a = 1\n"}, "c1")
        self.cfg = make_cfg(self.tmp, origin)
        self.repo, self.head = gitrepo.sync(self.cfg)
        self.finding = {"regra": "EF-2", "severity": "high", "file": "a.ts", "line": 1,
                        "title": "T", "description": "D", "suggestion": "S"}

    def test_agent_reads_file_then_reports(self):
        client = FakeClient([
            [call("read_file", path="a.ts")],
            [call("report_findings", resumo="ok", findings=[self.finding], verificados=["x"])],
        ])
        out = llm.run_agent(client, self.cfg, self.repo, self.head, "revisão", [], "u", llm.REPORT_FINDINGS)
        self.assertEqual(out["findings"][0]["title"], "T")
        self.assertEqual(client.turns, [])

    def test_agent_stops_at_turn_limit(self):
        client = FakeClient([[call("list_dir", path="")]] * 6)  # 4 voltas + 2 de cobrança da entrega
        with self.assertRaises(RuntimeError):
            llm.run_agent(client, self.cfg, self.repo, self.head, "revisão", [], "u", llm.REPORT_FINDINGS)

    def test_verification_drops_refuted_and_keeps_uncertain(self):
        other = {**self.finding, "title": "U"}
        client = FakeClient([[call("report_verdicts", verdicts=[
            {"index": 0, "verdict": "refuted", "motivo": "tratado em a.ts:1"},
            {"index": 1, "verdict": "uncertain", "motivo": "depende do painel"},
        ])]])
        kept = llm._verify(client, self.cfg, self.repo, self.head, "diff", [self.finding, other])
        self.assertEqual([f["title"] for f in kept], ["U"])
        self.assertIn("inconclusiva", kept[0]["description"])

    def test_verification_adjusts_severity_and_keeps_unjudged(self):
        other = {**self.finding, "title": "U"}
        client = FakeClient([[call("report_verdicts", verdicts=[
            {"index": 0, "verdict": "confirmed", "severity": "low", "motivo": "ok"},
        ])]])
        kept = llm._verify(client, self.cfg, self.repo, self.head, "diff", [self.finding, other])
        self.assertEqual([f["severity"] for f in kept], ["low", "high"])  # sem veredito = mantém


if __name__ == "__main__":
    unittest.main()


from reviewer import profile as _profile  # noqa: E402

RULES = _profile.load(TEST_PROFILE).rules


class RulesSelectionTest(unittest.TestCase):
    def test_full_checklist_when_no_groups(self):
        text = llm.rules_text(RULES, None)
        for rid in ("EF-1", "DB-1", "SQL-1", "FE-1", "Fora do escopo"):
            self.assertIn(rid, text)

    def test_edge_function_unit_gets_only_its_rules(self):
        from reviewer import units
        text = llm.rules_text(RULES, units.rule_groups("supabase/functions/foo"))
        self.assertIn("EF-2", text)
        self.assertIn("DB-2", text)
        self.assertNotIn("SQL-1", text)
        self.assertNotIn("FE-1", text)
        self.assertIn("Fora do escopo", text)  # sempre presente
        self.assertIn("Cada regra tem um ID", text)  # cabeçalho sempre presente

    def test_migration_and_frontend_units(self):
        from reviewer import units
        mig = llm.rules_text(RULES, units.rule_groups("supabase/migrations#3"))
        self.assertIn("SQL-1", mig)
        self.assertNotIn("EF-1", mig)
        fe = llm.rules_text(RULES, units.rule_groups("src/pages"))
        self.assertIn("FE-1", fe)
        self.assertNotIn("SQL-1", fe)
        self.assertNotIn("EF-1", fe)

    def test_unknown_unit_gets_everything(self):
        from reviewer import units
        self.assertIsNone(units.rule_groups("(raiz)"))

    def test_no_section_is_orphaned_by_the_mapping(self):
        """Toda seção do rules.md deve ser alcançável por algum tipo de unidade."""
        from reviewer import units
        used = set().union(*(units.rule_groups(k) for k in
                             ("supabase/functions/x", "supabase/migrations", "src/lib")))
        full = llm.rules_text(RULES, None)
        sections = set(re.findall(r"(?m)^#### (\w+) — ", full))
        self.assertEqual(sections - used, set())


class RealClientTest(unittest.TestCase):
    def test_make_client_builds_the_real_sdk_client(self):
        """Os outros testes simulam o cliente; este garante que o cliente REAL aceita as nossas opções."""
        cfg = make_cfg(Path(tempfile.mkdtemp()), Path("."))
        client = llm.make_client(cfg)
        self.assertEqual(client.max_retries, 3)
        self.assertEqual(client.timeout.read, 90.0)
        self.assertEqual(client.timeout.connect, 15.0)


class StreamRetryTest(AgentLoopTest):
    """Reaproveita o repositório de teste de AgentLoopTest (herda o setUp; os testes da base rodam de novo, é barato)."""

    def run_agent(self, client):
        with mock.patch("time.sleep"):
            return llm.run_agent(client, self.cfg, self.repo, self.head, "revisão", [], "u", llm.REPORT_FINDINGS)

    def final_turn(self):
        return [call("report_findings", resumo="ok", findings=[], verificados=[])]

    def test_connection_dropped_mid_stream_is_retried(self):
        import httpx2
        client = FakeClient([httpx2.ReadTimeout("The read operation timed out"), self.final_turn()])
        out = self.run_agent(client)
        self.assertEqual(out["findings"], [])
        self.assertEqual(client.turns, [])  # as duas respostas foram consumidas: houve nova tentativa

    def test_gives_up_after_three_attempts(self):
        import httpx2
        err = httpx2.ReadTimeout("The read operation timed out")
        client = FakeClient([err, err, err, self.final_turn()])
        with self.assertRaises(httpx2.ReadTimeout):
            self.run_agent(client)
        self.assertEqual(len(client.turns), 1)  # a 4ª resposta nunca foi pedida

    def test_non_network_errors_are_not_retried(self):
        client = FakeClient([ValueError("bug"), self.final_turn()])
        with self.assertRaises(ValueError):
            self.run_agent(client)
        self.assertEqual(len(client.turns), 1)

    def test_tool_results_survive_a_retry(self):
        import httpx2
        client = FakeClient([[call("read_file", path="a.ts")], httpx2.ConnectError("caiu"), self.final_turn()])
        self.assertEqual(self.run_agent(client)["findings"], [])


class NetworkErrorExplainTest(unittest.TestCase):
    def test_httpx2_errors_are_recognised_and_translated(self):
        import httpx2
        from reviewer import errors
        for exc in (httpx2.ReadTimeout("The read operation timed out"), httpx2.ConnectError("x"),
                    httpx2.RemoteProtocolError("x"), TimeoutError("x")):
            self.assertTrue(errors.is_transient(exc), exc)
            title, _detail, hint = errors.explain(exc)
            self.assertTrue(title.startswith("Falha de rede"), title)
            self.assertIn("curl", hint)

    def test_timeout_and_dropped_connection_have_distinct_titles(self):
        import httpx2
        from reviewer import errors
        self.assertIn("tempo", errors.explain(httpx2.ReadTimeout("The read operation timed out"))[0])
        self.assertIn("interrompida", errors.explain(httpx2.RemoteProtocolError("peer closed"))[0])

    def test_bugs_and_auth_errors_are_not_transient(self):
        from reviewer import errors
        self.assertFalse(errors.is_transient(ValueError("x")))
        self.assertFalse(errors.is_transient(KeyError("x")))


class ReadFileRangeTest(ToolsTest):
    def test_end_line_limits_the_range(self):
        out = self.run_tool("read_file", path="supabase/functions/foo/index.ts", start_line=1, end_line=1)
        self.assertIn("import { x }", out)
        self.assertNotIn("const a = 1", out)
        self.assertIn("linhas='1-1'", out)


class StatsAndReportTest(unittest.TestCase):
    def test_stats_accumulate_across_turns(self):
        llm.reset_stats()
        tmp = Path(tempfile.mkdtemp())
        origin = tmp / "o"
        origin.mkdir()
        git(origin, "init", "-q", "-b", "main")
        commit(origin, {"a.ts": "x\n"}, "c1")
        cfg = make_cfg(tmp, origin)
        repo, head = gitrepo.sync(cfg)
        client = FakeClient([[call("list_dir", path="")],
                             [call("report_findings", resumo="a", findings=[], verificados=[])]])
        llm.run_agent(client, cfg, repo, head, "revisão", [], "u", llm.REPORT_FINDINGS)
        self.assertEqual(llm.STATS["turns"], 2)
        self.assertIn("2 chamadas", llm.stats_summary())

    def test_simulation_title_and_deduped_verified_items(self):
        from reviewer import report
        md = report.markdown_full(0, "", "a" * 40, [], ["x — EF-1: ok"])
        self.assertTrue(md.startswith("# Simulação"))

    def test_prompts_ask_for_file_in_verified_items(self):
        for name in ("review.md",):
            text = (Path(__file__).resolve().parent.parent / "prompts" / name).read_text(encoding="utf-8")
            self.assertIn("arquivo — REGRA: frase", text)
            self.assertIn("comentário no código diz que o comportamento é intencional", text)
