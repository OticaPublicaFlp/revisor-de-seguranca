import dataclasses
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from reviewer import gitrepo, main, report
from reviewer.config import Config
from reviewer.db import Store, fingerprint

CHAT = 42


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"})


def commit(origin: Path, files: dict, msg: str):
    for name, content in files.items():
        p = origin / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    git(origin, "add", "-A")
    git(origin, "commit", "-qm", msg)


class FakeTelegram:
    def __init__(self):
        self.sent, self.answers, self.cleared = [], [], []

    def send_document(self, name, content): self.sent.append(("doc", name))
    def send_summary(self, text, run_id, slack_enabled=True): self.sent.append(("summary", run_id))
    def send_text(self, text): self.sent.append(("text", text))
    def answer_callback(self, cid, text): self.answers.append(text)
    def clear_keyboard(self, mid, footer): self.cleared.append(footer)


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin"
        self.origin.mkdir()
        git(self.origin, "init", "-q", "-b", "main")
        commit(self.origin, {"a.ts": "const a = 1\n"}, "c1")
        commit(self.origin, {"b.ts": "const b = 2\n", "package-lock.json": "{}\n"}, "c2")
        self.cfg = Config(
            repo_url=str(self.origin), repo_branch="main", repo_token="", cron_expr="0 8 * * *",
            tz="America/Sao_Paulo", initial_lookback=20, anthropic_api_key="x", model="m",
            max_chunks=6, chunk_chars=60000, max_output_tokens=100, max_turns=4, verify=True, telegram_token="t",
            telegram_chat_id=CHAT, telegram_approver_ids=frozenset({CHAT}), slack_token="s", slack_channel="C1", slack_include_details=False,
            data_dir=self.tmp / "data", mode="diff", deps_check=False,
            profile_name=str(Path(__file__).resolve().parent / "fixtures" / "perfil"),
        )
        self.cfg.data_dir.mkdir()
        self.store = Store(self.cfg.data_dir / "state.db")
        self.tg = FakeTelegram()

    FINDING = {"severity": "high", "file": "b.ts", "line": 1, "title": "Falta validação",
               "description": "d", "suggestion": "s"}

    def test_diff_ignores_lockfiles(self):
        repo, head = gitrepo.sync(self.cfg)
        base = gitrepo.resolve_base(self.cfg, repo, None, head)
        names = [n for n, _ in gitrepo.diff_by_file(self.cfg, repo, base, head)]
        self.assertNotIn("package-lock.json", names)
        self.assertIn("b.ts", names)

    def test_full_flow_dedup_and_approval(self):
        with mock.patch("reviewer.llm.review_chunks", return_value=([dict(self.FINDING)], [])):
            main.run_review(self.cfg, self.store, self.tg)
        self.assertEqual([k for k, _ in self.tg.sent], ["doc", "summary"])
        run_id = self.tg.sent[1][1]

        # Mesmo achado numa execução seguinte (novo commit) não é reenviado
        commit(self.origin, {"c.ts": "x\n"}, "c3")
        self.tg.sent.clear()
        with mock.patch("reviewer.llm.review_chunks", return_value=([dict(self.FINDING)], [])):
            main.run_review(self.cfg, self.store, self.tg)
        self.assertEqual([k for k, _ in self.tg.sent], ["text"])

        cq = {"id": "1", "data": f"slack:{run_id}", "from": {"id": CHAT},
              "message": {"message_id": 7, "chat": {"id": CHAT}}}
        with mock.patch("reviewer.slack.post_message") as post:
            main.handle_callback(self.cfg, self.store, self.tg, cq)
            main.handle_callback(self.cfg, self.store, self.tg, cq)  # clique duplo
        self.assertEqual(post.call_count, 1)
        self.assertEqual(self.tg.answers[-1], "Já decidido.")

    def test_unauthorized_callback_is_ignored(self):
        with mock.patch("reviewer.llm.review_chunks", return_value=([dict(self.FINDING)], [])):
            main.run_review(self.cfg, self.store, self.tg)
        run_id = self.tg.sent[1][1]
        cq = {"id": "1", "data": f"slack:{run_id}", "from": {"id": 999},
              "message": {"message_id": 7, "chat": {"id": 999}}}
        with mock.patch("reviewer.slack.post_message") as post:
            main.handle_callback(self.cfg, self.store, self.tg, cq)
        post.assert_not_called()
        self.assertEqual(self.store.get_run(run_id)["status"], "pending")

    def test_slack_failure_reverts_to_pending(self):
        with mock.patch("reviewer.llm.review_chunks", return_value=([dict(self.FINDING)], [])):
            main.run_review(self.cfg, self.store, self.tg)
        run_id = self.tg.sent[1][1]
        cq = {"id": "1", "data": f"slack:{run_id}", "from": {"id": CHAT},
              "message": {"message_id": 7, "chat": {"id": CHAT}}}
        with mock.patch("reviewer.slack.post_message", side_effect=RuntimeError("boom")):
            main.handle_callback(self.cfg, self.store, self.tg, cq)
        self.assertEqual(self.store.get_run(run_id)["status"], "pending")

    def test_failed_review_does_not_advance_sha(self):
        with mock.patch("reviewer.llm.review_chunks", side_effect=RuntimeError("api down")):
            with self.assertRaises(RuntimeError):
                main.run_review(self.cfg, self.store, self.tg)
        self.assertIsNone(self.store.get("last_sha"))

    def _pending_run(self):
        with mock.patch("reviewer.llm.review_chunks", return_value=([dict(self.FINDING)], [])):
            main.run_review(self.cfg, self.store, self.tg)
        return self.tg.sent[1][1]

    @staticmethod
    def _click(run_id, user, chat=CHAT):
        return {"id": "1", "data": f"slack:{run_id}", "from": {"id": user},
                "message": {"message_id": 7, "chat": {"id": chat}}}

    def test_group_without_approver_list_lets_any_member_approve(self):
        cfg = dataclasses.replace(self.cfg, telegram_approver_ids=frozenset())
        run_id = self._pending_run()
        with mock.patch("reviewer.slack.post_message") as post:
            main.handle_callback(cfg, self.store, self.tg, self._click(run_id, user=555))
        post.assert_called_once()

    def test_click_from_another_chat_is_denied_even_without_list(self):
        cfg = dataclasses.replace(self.cfg, telegram_approver_ids=frozenset())
        run_id = self._pending_run()
        with mock.patch("reviewer.slack.post_message") as post:
            main.handle_callback(cfg, self.store, self.tg, self._click(run_id, user=555, chat=-999))
        post.assert_not_called()

    def test_approver_list_restricts_when_set(self):
        cfg = dataclasses.replace(self.cfg, telegram_approver_ids=frozenset({1}))
        run_id = self._pending_run()
        with mock.patch("reviewer.slack.post_message") as post:
            main.handle_callback(cfg, self.store, self.tg, self._click(run_id, user=555))
        post.assert_not_called()

    def test_ssh_key_sets_isolated_ssh_command(self):
        cfg = dataclasses.replace(self.cfg, repo_ssh_key="/tmp/my key")
        cmd = gitrepo._env(cfg)["GIT_SSH_COMMAND"]
        self.assertIn("IdentitiesOnly=yes", cmd)
        self.assertIn("-F /dev/null", cmd)
        self.assertIn("'/tmp/my key'", cmd)

    def test_fingerprint_ignores_line(self):
        a, b = dict(self.FINDING), {**self.FINDING, "line": 99}
        self.assertEqual(fingerprint(a), fingerprint(b))

    def test_slack_message_hides_details_by_default(self):
        msg = report.slack_message(1, "a" * 7, "b" * 7, [self.FINDING], include_details=False)
        self.assertNotIn("> d", msg)


if __name__ == "__main__":
    unittest.main()


class RetryTest(unittest.TestCase):
    NET = gitrepo.GitError("git fetch falhou: fatal: unable to access 'https://x': Failed to connect")

    def test_retries_network_errors_then_succeeds(self):
        calls = []
        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise self.NET
            return "ok"
        with mock.patch("time.sleep") as sleep:
            self.assertEqual(gitrepo._with_retry(flaky), "ok")
        self.assertEqual(len(calls), 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [5, 20])

    def test_gives_up_after_three_attempts(self):
        with mock.patch("time.sleep"):
            with self.assertRaises(gitrepo.GitError):
                gitrepo._with_retry(mock.Mock(side_effect=self.NET))

    def test_non_network_errors_are_not_retried(self):
        fn = mock.Mock(side_effect=gitrepo.GitError("git fetch falhou: fatal: couldn't find remote ref main"))
        with mock.patch("time.sleep") as sleep:
            with self.assertRaises(gitrepo.GitError):
                gitrepo._with_retry(fn)
        self.assertEqual(fn.call_count, 1)
        sleep.assert_not_called()
