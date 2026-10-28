import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from reviewer import gitrepo, main, ui, units
from reviewer.db import Store, now_iso
from test_pipeline import FakeTelegram, commit, git
from test_tools_llm import make_cfg

ui.enabled = False
FINDING = {"regra": "EF-2", "severity": "high", "file": "supabase/functions/a/index.ts", "line": 1,
           "title": "Sem auth", "description": "d", "suggestion": "s"}


class CodeModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin"
        self.origin.mkdir()
        git(self.origin, "init", "-q", "-b", "main")
        commit(self.origin, {
            "supabase/functions/a/index.ts": "export const a = 1\n",
            "supabase/functions/b/index.ts": "export const b = 1\n",
            "supabase/functions/_shared/x.ts": "export const x = 1\n",
            "supabase/migrations/001.sql": "select 1;\n",
            "src/lib/util.ts": "export const u = 1\n",
            "src/lib/util.test.ts": "test\n",
            "src/integrations/supabase/types.ts": "generated\n",
            "package-lock.json": "{}\n",
            "README.md": "doc\n",
            ".env": "SECRET=1\n",
        }, "c1")
        self.cfg = make_cfg(self.tmp, self.origin, mode="code", max_units=2)
        self.store = Store(self.cfg.data_dir / "state.db")
        self.tg = FakeTelegram()
        self.repo, self.head = gitrepo.sync(self.cfg)
        # Estes testes tratam só das unidades de código; os temas têm testes próprios.
        patcher = mock.patch("reviewer.themes.build", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)

    def keys(self):
        return {u.key for u in units.build(self.cfg, self.repo, self.head)}

    def test_units_group_and_filter(self):
        self.assertEqual(self.keys(), {"supabase/functions/a", "supabase/functions/b",
                                       "supabase/functions/_shared", "supabase/migrations", "src/lib"})

    def test_selection_prioritizes_edge_functions_and_caps(self):
        plan = units.select({}, units.build(self.cfg, self.repo, self.head), self.cfg)
        self.assertEqual(len(plan), 2)
        self.assertTrue(all(u.key.startswith("supabase/functions") for u, _ in plan))

    def test_unchanged_unit_is_skipped_until_recheck_age(self):
        all_units = units.build(self.cfg, self.repo, self.head)
        old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        reviewed = {u.key: (u.fingerprint, now_iso()) for u in all_units}
        self.assertEqual(units.select(reviewed, all_units, self.cfg), [])
        reviewed["src/lib"] = (reviewed["src/lib"][0], old)
        plan = units.select(reviewed, all_units, self.cfg)
        self.assertEqual([(u.key, "40 dias" in r) for u, r in plan], [("src/lib", True)])

    def test_changed_unit_comes_first(self):
        all_units = units.build(self.cfg, self.repo, self.head)
        reviewed = {u.key: (u.fingerprint, now_iso()) for u in all_units}
        reviewed["src/lib"] = ("antiga", now_iso())
        plan = units.select(reviewed, all_units, self.cfg)
        self.assertEqual(plan[0][0].key, "src/lib")
        self.assertIn("alterado", plan[0][1])

    def test_render_numbers_lines(self):
        u = next(u for u in units.build(self.cfg, self.repo, self.head) if u.key == "src/lib")
        text = units.render(self.cfg, self.repo, self.head, u)
        self.assertIn("    1  export const u = 1", text)
        self.assertNotIn("test", text.replace("util.ts", ""))

    def test_full_run_marks_units_and_second_run_picks_next(self):
        with mock.patch("reviewer.llm.review_chunks", return_value=([dict(FINDING)], ["EF-10 ok"])) as rv:
            main.run_review(self.cfg, self.store, self.tg)
        self.assertEqual(rv.call_count, 2)
        self.assertEqual(rv.call_args.kwargs["kind"], "code")
        self.assertEqual(len(self.store.get_units()), 2)
        self.assertEqual([k for k, _ in self.tg.sent], ["doc", "summary"])

        self.tg.sent.clear()
        with mock.patch("reviewer.llm.review_chunks", return_value=([], [])) as rv:
            main.run_review(self.cfg, self.store, self.tg)
        self.assertEqual(len(self.store.get_units()), 4)  # avançou para as próximas unidades

    def test_all_up_to_date_sends_heartbeat_without_model(self):
        for u in units.build(self.cfg, self.repo, self.head):
            self.store.mark_unit(u.key, u.fingerprint)
        with mock.patch("reviewer.llm.review_chunks") as rv:
            main.run_review(self.cfg, self.store, self.tg)
        rv.assert_not_called()
        self.assertEqual([k for k, _ in self.tg.sent], ["text"])

    def test_dry_run_does_not_mark_units(self):
        with mock.patch("reviewer.llm.review_chunks", return_value=([], [])):
            main.run_review(self.cfg, self.store, None, dry_run=True)
        self.assertEqual(self.store.get_units(), {})

    def test_failure_midway_keeps_completed_units(self):
        results = [([dict(FINDING)], []), RuntimeError("api down")]
        with mock.patch("reviewer.llm.review_chunks", side_effect=results), \
                mock.patch("reviewer.report.telegram_summary", wraps=main.report.telegram_summary) as summary:
            main.run_review(self.cfg, self.store, self.tg)
        self.assertEqual(len(self.store.get_units()), 1)  # só a unidade concluída foi marcada
        note = summary.call_args.kwargs["note"]
        self.assertIn("Cobertura: 1/", note)
        self.assertIn("Falhou em", note)

    def test_failure_on_first_unit_raises_and_marks_nothing(self):
        with mock.patch("reviewer.llm.review_chunks", side_effect=RuntimeError("api down")):
            with self.assertRaises(RuntimeError):
                main.run_review(self.cfg, self.store, self.tg)
        self.assertEqual(self.store.get_units(), {})


if __name__ == "__main__":
    unittest.main()
