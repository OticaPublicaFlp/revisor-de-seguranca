import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from reviewer import bench, deps, errors, gitrepo, llm, main, prechecks, themes, ui, units
from reviewer.db import Store
from test_pipeline import FakeTelegram, commit, git
from test_tools_llm import FakeClient, call, make_cfg

ui.enabled = False


def jwt(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJIUzI1NiJ9.{body}.c2lnbmF0dXJlc2lnbmF0dXJl"


class RepoCase(unittest.TestCase):
    FILES: dict = {}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        origin = self.tmp / "origin"
        origin.mkdir()
        git(origin, "init", "-q", "-b", "main")
        commit(origin, self.FILES, "c1")
        self.cfg = make_cfg(self.tmp, origin, mode="code", max_units=2)
        self.repo, self.head = gitrepo.sync(self.cfg)

    def hints(self):
        return prechecks.run(self.cfg, self.repo, self.head)


class OpusRequestTest(RepoCase):
    FILES = {"a.ts": "x\n"}

    def final(self):
        return [call("report_findings", resumo="ok", findings=[], verificados=[])]

    def test_request_uses_supported_opus_parameters(self):
        client = FakeClient([self.final()])
        llm.run_agent(client, self.cfg, self.repo, self.head, "revisão", [], "u", llm.REPORT_FINDINGS)
        sent = client.calls[0]
        self.assertEqual(sent["tool_choice"], {"type": "auto"})  # forçado dá 400 no Opus 5.5
        self.assertEqual(sent["output_config"], {"effort": "medium"})
        self.assertEqual(sent["thinking"]["type"], "adaptive")
        self.assertEqual(sent["fallbacks"], "default")
        self.assertIn(llm.FALLBACK_BETA, sent["betas"])
        self.assertNotIn("temperature", sent)

    def test_over_limit_reads_are_refused_and_delivery_is_requested(self):
        client = FakeClient([[call("list_dir", path="")]] * 4 + [self.final()])
        with mock.patch("reviewer.tools.execute", wraps=llm.tools.execute) as execute:
            llm.run_agent(client, self.cfg, self.repo, self.head, "revisão", [], "u", llm.REPORT_FINDINGS)
        self.assertEqual(execute.call_count, 3)  # a 4ª leitura (no limite) não foi executada
        last_user = client.calls[-1]["messages"][-1]["content"]
        self.assertIn("erro: Limite de consultas", last_user[0]["content"])
        self.assertTrue(any(b.get("type") == "text" and "report_findings" in b["text"] for b in last_user))

    def test_history_is_append_only(self):
        """O Opus 5.5 invalida o raciocínio se o histórico for editado: cada chamada estende a anterior."""
        client = FakeClient([[call("list_dir", path="")], [call("list_dir", tool_id="t2", path="")], self.final()])
        llm.run_agent(client, self.cfg, self.repo, self.head, "revisão", [], "u", llm.REPORT_FINDINGS)
        snapshots = [[json.dumps(m, default=str) for m in c["messages"]] for c in client.calls]
        for before, after in zip(snapshots, snapshots[1:]):
            self.assertEqual(after[:len(before)], before)

    def test_refusal_raises_explained_error(self):
        client = FakeClient([self.final()])
        refused = SimpleNamespace(content=[], stop_reason="refusal", stop_details=SimpleNamespace(category="cyber"),
                                  usage=SimpleNamespace(input_tokens=1, output_tokens=1))
        with mock.patch.object(llm, "_request", return_value=refused):
            with self.assertRaises(errors.ModelRefusal) as ctx:
                llm.run_agent(client, self.cfg, self.repo, self.head, "revisão", [], "u", llm.REPORT_FINDINGS)
        self.assertIn("cyber", errors.explain(ctx.exception)[0])

    def test_cost_estimate_uses_opus_prices(self):
        llm.reset_stats()
        usage = SimpleNamespace(input_tokens=1_000_000, output_tokens=100_000, cache_read_input_tokens=0,
                                cache_creation_input_tokens=0)
        llm._account("claude-opus-5-5", usage, 1.0)
        self.assertAlmostEqual(llm.STATS["usd"], 4.0 + 2.0)
        self.assertIn("US$ 6,00", llm.stats_summary())


class SqlStateTest(RepoCase):
    FILES = {
        "supabase/migrations/20250101000000_old.sql": (
            "-- função antiga: nasce executável por PUBLIC\n"
            "CREATE OR REPLACE FUNCTION public.leak(p uuid) RETURNS int LANGUAGE sql SECURITY DEFINER AS $$\n"
            "  DELETE FROM t WHERE id = p; SELECT 1;\n$$;\n"
            "CREATE POLICY \"aberta\" ON public.t FOR SELECT TO anon USING (true);\n"
            "CREATE POLICY \"sem_to\" ON public.t FOR SELECT USING (organization_id = 1);\n"
            "GRANT SELECT ON public.t TO anon;\n"),
        "supabase/migrations/20250102000000_fix_grant.sql": "REVOKE ALL ON public.t FROM anon;\n",
        "supabase/migrations/20261022000000_new.sql": (
            "CREATE FUNCTION public.newer() RETURNS void LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$\n"
            "  INSERT INTO t VALUES (1);\n$$;\n"),
    }

    def test_old_function_is_public_new_one_is_not(self):
        _hints, ctx = self.hints()
        fns = ctx["sql"]["functions"]
        self.assertEqual(fns["public.leak"]["exec"], ["public"])
        self.assertEqual(fns["public.newer"]["exec"], [])  # default privileges do projeto

    def test_hints_reflect_final_state(self):
        hints, _ = self.hints()
        texts = [h.text() for h in hints]
        self.assertTrue(any("public.leak é SECURITY DEFINER e executável por public" in t for t in texts))
        self.assertTrue(any("public.leak" in t and "sem search_path" in t for t in texts))
        self.assertTrue(any("policy 'aberta'" in t and "anon" in t for t in texts))
        self.assertFalse(any("sem_to" in t for t in texts))          # sem TO não vira pista
        self.assertFalse(any("GRANT SELECT ON public.t" in t for t in texts))  # revogado depois
        self.assertFalse(any("public.newer" in t for t in texts))     # ninguém do app executa


class DynamicRevokeTest(RepoCase):
    FILES = {
        "supabase/migrations/20250101000000_old.sql":
            "CREATE FUNCTION public.f() RETURNS int LANGUAGE sql SECURITY DEFINER AS $$ SELECT 1 $$;\n",
        "supabase/migrations/20250201000000_revoke_loop.sql": (
            "DO $$ DECLARE r record; BEGIN\n"
            "  FOR r IN SELECT p.oid::regprocedure AS sig FROM pg_proc p LOOP\n"
            "    EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC, anon', r.sig);\n"
            "  END LOOP; END $$;\n"),
    }

    def test_revoke_inside_do_block_is_applied(self):
        _hints, ctx = self.hints()
        self.assertEqual(ctx["sql"]["functions"]["public.f"]["exec"], [])


class EdgeAndSecretsTest(RepoCase):
    FILES = {
        "supabase/config.toml": "[functions.open]\nverify_jwt = false\n[functions.safe]\nverify_jwt = false\n",
        "supabase/functions/open/index.ts": "const c = createClient(url, Deno.env.get('SUPABASE_SERVICE_ROLE_KEY'))\n",
        "supabase/functions/safe/index.ts": "if (!(await requireInternal(req))) return forbidden()\n",
        "supabase/functions/_shared/pem.ts": "key.replace('-----BEGIN " + "PRIVATE KEY-----', '')\n",
        "src/config.ts": f"export const ADMIN = '{jwt({'role': 'service_role'})}'\n",
        "src/public.ts": f"export const ANON = '{jwt({'role': 'anon'})}'\n",
        "src/lib/__tests__/a.test.ts": "const t = '123456789:" + "AA" + "EhBP0abcdefghijklmnopqrstuvwxyz12'\n",
    }

    def test_edge_hints(self):
        texts = [h.text() for h in self.hints()[0]]
        self.assertTrue(any("open/index.ts" in t and "verify_jwt=false" in t for t in texts))
        self.assertFalse(any("safe/index.ts" in t and "verify_jwt=false" in t for t in texts))
        self.assertTrue(any("[DB-2]" in t and "open/index.ts" in t for t in texts))

    def test_secrets_detected_without_values(self):
        hints, _ = self.hints()
        secret = [h for h in hints if h.rule == "EF-6"]
        files = {h.file for h in secret}
        self.assertIn("src/config.ts", files)                 # JWT de service_role
        self.assertNotIn("src/public.ts", files)              # chave anon é pública
        self.assertNotIn("supabase/functions/_shared/pem.ts", files)  # só manipula o cabeçalho PEM
        test_hint = next(h for h in secret if "__tests__" in h.file)
        self.assertIn("arquivo de teste", test_hint.message)
        for h in secret:
            self.assertNotIn("AAEhBP0", h.text())
            self.assertNotIn("eyJ", h.text())


class DepsTest(RepoCase):
    FILES = {"package-lock.json": json.dumps({"lockfileVersion": 3, "packages": {
        "": {},
        "node_modules/prod": {"version": "1.0.0", "resolved": "https://registry.npmjs.org/prod/-/prod-1.0.0.tgz"},
        "node_modules/devonly": {"version": "2.0.0", "dev": True,
                                 "resolved": "https://registry.npmjs.org/devonly/-/devonly-2.0.0.tgz"},
        "node_modules/vendor": {"version": "0.20.3", "resolved": "https://cdn.example.com/vendor-0.20.3.tgz"},
        "node_modules/clean": {"version": "3.0.0", "resolved": "https://registry.npmjs.org/clean/-/clean-3.0.0.tgz"},
    }})}

    def fake_http(self):
        vulns = {"prod": ["GHSA-1"], "devonly": ["GHSA-2"], "vendor": ["GHSA-3"]}
        detail = {"GHSA-1": "CRITICAL", "GHSA-2": "HIGH", "GHSA-3": "HIGH"}

        class Resp:
            def __init__(self, data):
                self.data = data

            def raise_for_status(self):
                return self

            def json(self):
                return self.data

        class Http:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, json):
                return Resp({"results": [{"vulns": [{"id": i} for i in vulns.get(q["package"]["name"], [])]}
                                         for q in json["queries"]]})

            def get(self, url):
                vid = url.rsplit("/", 1)[1]
                name = {"GHSA-1": "prod", "GHSA-2": "devonly", "GHSA-3": "vendor"}[vid]
                return Resp({"id": vid, "summary": "x", "database_specific": {"severity": detail[vid]},
                             "affected": [{"package": {"name": name},
                                           "ranges": [{"events": [{"introduced": "0"}, {"fixed": "9.9.9"}]}]}]})
        return Http()

    def test_severity_rules(self):
        with mock.patch("httpx.Client", return_value=self.fake_http()):
            found = {f["title"].split("@")[0]: f for f in deps.findings(self.cfg, self.repo, self.head)}
        self.assertEqual(set(found), {"prod", "devonly", "vendor"})
        self.assertEqual(found["prod"]["severity"], "critical")
        self.assertEqual(found["devonly"]["severity"], "low")   # só desenvolvimento
        self.assertEqual(found["vendor"]["severity"], "info")   # fora do registro npm
        self.assertIn("fora do registro npm", found["vendor"]["description"])
        self.assertIn("9.9.9", found["prod"]["suggestion"])

    def test_network_failure_does_not_break_review(self):
        import httpx
        with mock.patch("httpx.Client", side_effect=httpx.ConnectError("off")):
            self.assertEqual(deps.findings(self.cfg, self.repo, self.head), [])


class ThemesAndDualTest(RepoCase):
    FILES = {
        "supabase/config.toml": "[functions.pub]\nverify_jwt = false\n",
        "supabase/functions/pub/index.ts": "const r = await supa.from('customers').select('*')\n",
        "supabase/migrations/20250101000000_a.sql": "select 1;\n",
        "src/pages/PublicSurvey.tsx": "export default 1\n",
        "src/lib/util.ts": "export const u = 1\n",
    }

    def test_inventories_and_rule_groups(self):
        _hints, ctx = self.hints()
        built = {t.key: t for t in themes.build(ctx, units.src_files(self.cfg, self.repo, self.head), self.cfg.profile)}
        self.assertEqual(set(built), {f"tema:{k}" for k in themes.THEMES})
        self.assertIn("| pub | FALSE | NENHUMA |", built["tema:acesso-publico"].inventory)
        self.assertIn("src/pages/PublicSurvey.tsx", built["tema:acesso-publico"].inventory)
        self.assertIn("pub", built["tema:dados-sensiveis"].inventory)
        self.assertEqual(units.rule_groups("tema:funcoes-privilegiadas"), frozenset({"SQL", "DB"}))

    def test_code_mode_reviews_themes_first_with_dual_review_and_hints(self):
        store = Store(self.cfg.data_dir / "state.db")
        with mock.patch("reviewer.llm.review_chunks", return_value=([], [])) as rv:
            main.run_review(self.cfg, store, FakeTelegram())
        kinds = [c.kwargs["kind"] for c in rv.call_args_list]
        self.assertEqual(kinds, ["theme", "theme"])
        self.assertTrue(all(c.kwargs["dual"] for c in rv.call_args_list))  # DUAL_REVIEW=critical
        self.assertTrue(any(c.kwargs["hints"] for c in rv.call_args_list))

    def test_dual_review_policy(self):
        import dataclasses
        edge = units.Unit("supabase/functions/x", (), prio=0)
        front = units.Unit("src/pages", (), prio=3)
        crit = dataclasses.replace(self.cfg, dual_review="critical")
        self.assertTrue(main._dual(crit, edge))
        self.assertFalse(main._dual(crit, front))
        self.assertTrue(main._dual(dataclasses.replace(self.cfg, dual_review="all"), front))
        self.assertFalse(main._dual(dataclasses.replace(self.cfg, dual_review="off"), edge))


class BenchMatchTest(unittest.TestCase):
    CASE = {"match_any": ["chave anônima", "x-cron-secret"], "match_file": "process-automated", "severity_min": "high"}

    def finding(self, title, file):
        return {"title": title, "description": "", "file": file, "severity": "high"}

    def test_match_levels_and_accent_insensitive(self):
        hit = self.finding("Cron aceita a CHAVE ANONIMA", "supabase/functions/process-automated-notifications/index.ts")
        self.assertEqual(bench.match(self.CASE, [hit])[0], "achou")
        elsewhere = self.finding("Usa x-cron-secret errado", "supabase/functions/outra/index.ts")
        self.assertEqual(bench.match(self.CASE, [elsewhere])[0], "parcial")
        self.assertEqual(bench.match(self.CASE, [self.finding("Outra coisa", "a.ts")])[0], "perdeu")

    def test_every_cases_file_is_well_formed(self):
        """Valida os casos de qualquer perfil instalado (perfis próprios ficam fora do git)."""
        groups = {"EF", "DB", "SQL", "REL", "AI", "CX", "FE"}
        for path in (Path(__file__).resolve().parent.parent / "profiles").glob("*/casos.json"):
            for c in json.loads(path.read_text(encoding="utf-8"))["cases"]:
                self.assertTrue(c["files"] and c["match_any"] and c["prefix_commit"], (path, c["id"]))
                self.assertLessEqual(set(c["rule_groups"]), groups, (path, c["id"]))
                self.assertIn(c["severity_min"], ("high", "critical"), (path, c["id"]))


if __name__ == "__main__":
    unittest.main()
