import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from reviewer import llm, profile, ui, wizard
from test_tools_llm import TEST_PROFILE, make_cfg

ui.enabled = False
ROOT = Path(__file__).resolve().parent.parent


class ProfileTest(unittest.TestCase):
    def test_example_profile_is_complete(self):
        p = profile.load("exemplo")
        self.assertEqual(p.project, "AppExemplo")
        self.assertTrue(p.context and p.rules and p.examples)
        self.assertTrue(p.tenant_filter.search("organization_id"))
        # IDs que as pistas automáticas usam precisam existir no checklist do exemplo
        for rid in ("EF-2", "EF-6", "EF-7", "EF-9", "DB-2", "SQL-1", "SQL-2", "FE-7", "FE-10"):
            self.assertIn(f"**{rid} ", p.rules, rid)

    def test_missing_profile_has_clear_message(self):
        with self.assertRaises(SystemExit) as ctx:
            profile.load("nao-existe")
        self.assertIn("exemplo", str(ctx.exception))  # lista os disponíveis

    def test_invalid_tenant_regex_is_reported(self):
        tmp = Path(tempfile.mkdtemp()) / "ruim"
        tmp.mkdir()
        (tmp / "perfil.toml").write_text('nome = "x"\n[prechecks]\ntenant_filter = "("\n', encoding="utf-8")
        (tmp / "contexto.md").write_text("c", encoding="utf-8")
        (tmp / "regras.md").write_text("r", encoding="utf-8")
        with self.assertRaises(SystemExit) as ctx:
            profile.load(str(tmp))
        self.assertIn("tenant_filter", str(ctx.exception))

    def test_prompts_have_no_unfilled_placeholders(self):
        cfg = make_cfg(Path(tempfile.mkdtemp()), Path("."))
        for name in ("review", "attack", "verify"):
            text = llm._prompt(cfg, name, frozenset({"EF"}))[0]["text"]
            self.assertNotIn("{{", text, name)
            self.assertIn("ProjetoTeste", text, name)

    def test_generic_prompts_do_not_carry_project_specifics(self):
        """Os modelos em prompts/ são genéricos: o que é do projeto vem do perfil."""
        for name in ("review", "attack", "verify"):
            text = (ROOT / "prompts" / f"{name}.md").read_text(encoding="utf-8")
            self.assertIn("{{PROJETO}}", text, name)


class EnvFileTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.template = self.dir / ".env.example"
        self.template.write_text("# comentário\nA=\n# outro\nB=padrao\n", encoding="utf-8")
        self.env = self.dir / ".env"

    def test_write_keeps_template_order_and_restricts_permissions(self):
        wizard.write_env({"B": "2", "A": "1", "EXTRA": "x"}, self.env, self.template)
        text = self.env.read_text(encoding="utf-8")
        self.assertLess(text.index("A=1"), text.index("B=2"))
        self.assertIn("# comentário", text)
        self.assertIn("EXTRA=x", text)
        self.assertEqual(stat.S_IMODE(self.env.stat().st_mode), 0o600)
        self.assertEqual(wizard.read_env(self.env)["A"], "1")

    def test_previous_env_is_backed_up(self):
        self.env.write_text("A=antigo\n", encoding="utf-8")
        backup = wizard.write_env({"A": "novo"}, self.env, self.template)
        self.assertEqual(backup.read_text(encoding="utf-8"), "A=antigo\n")
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)

    def test_mask_never_shows_the_secret(self):
        self.assertEqual(wizard.mask(""), "(vazio)")
        self.assertEqual(wizard.mask("curto"), "••••")
        self.assertEqual(wizard.mask("sk-ant-api03-segredo-1234"), "••••1234")


class WizardFlowTest(unittest.TestCase):
    """Roda o assistente inteiro com respostas simuladas e sem rede."""

    REAL_ENV = ROOT / ".env"

    def setUp(self):
        # Trava: se algum teste gravar no .env de verdade, falha (já aconteceu durante o desenvolvimento).
        self.real_before = self.REAL_ENV.stat().st_mtime_ns if self.REAL_ENV.exists() else None
        self.real_backups = set(ROOT.glob(".env.bak-*"))

    def tearDown(self):
        after = self.REAL_ENV.stat().st_mtime_ns if self.REAL_ENV.exists() else None
        self.assertEqual(after, self.real_before, "o teste alterou o .env real!")
        self.assertEqual(set(ROOT.glob(".env.bak-*")), self.real_backups, "o teste criou backup do .env real!")

    def run_wizard(self, answers, secrets):
        tmp = Path(tempfile.mkdtemp())
        env = tmp / ".env"
        answers, secrets = iter(answers), iter(secrets)
        with mock.patch.object(wizard, "ENV", env), \
                mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("builtins.input", side_effect=lambda *_: next(answers)), \
                mock.patch("getpass.getpass", side_effect=lambda *_: next(secrets)), \
                mock.patch.object(wizard, "check_repo", return_value=(True, "ok")), \
                mock.patch.object(wizard, "check_anthropic", return_value=(True, "ok")), \
                mock.patch.object(wizard, "telegram_call", return_value={"username": "meu_bot", "id": 1}), \
                mock.patch.object(wizard, "telegram_scan", return_value=({-100: "Equipe"}, {7: "Ana"})), \
                mock.patch.object(wizard.profiles, "available", return_value=["exemplo"]), \
                mock.patch("builtins.print"):
            wizard.run_wizard()
        return wizard.read_env(env), env

    def test_full_setup_writes_validated_config(self):
        answers = [
            "1",                                        # perfil: exemplo
            "https://github.com/empresa/projeto.git",   # URL
            "",                                         # branch: main
            "2",                                        # acesso: token
            "1",                                        # modelo: Opus
            "",                                         # esforço: médio
            "", "3",                                    # revisão dupla: sim · 3 partes
            "",                                         # dependências: sim
            "s", "", "1", "",                           # Telegram: sim · Enter após /start · conversa 1 · todos aprovam
            "n",                                        # Slack: não
            "2", "",                                    # agenda: dias úteis · fuso padrão
            "",                                         # salvar: sim
        ]
        secrets = ["token-github-falso", "sk-ant-chavefalsa", "123:token-bot-falso"]
        values, env = self.run_wizard(answers, secrets)
        self.assertEqual(values["PROFILE"], "exemplo")
        self.assertEqual(values["REPO_TOKEN"], "token-github-falso")
        self.assertEqual(values["REVIEW_MODEL"], "claude-opus-5-5")
        self.assertEqual(values["EFFORT"], "medium")
        self.assertEqual(values["DUAL_REVIEW"], "critical")
        self.assertEqual(values["MAX_UNITS"], "3")
        self.assertEqual(values["TELEGRAM_CHAT_ID"], "-100")
        self.assertEqual(values["CRON_EXPR"], "0 8 * * 1-5")
        self.assertEqual(values["SLACK_BOT_TOKEN"], "")
        self.assertEqual(stat.S_IMODE(env.stat().st_mode), 0o600)

    def test_cancel_at_the_end_writes_nothing(self):
        answers = ["1", "https://github.com/e/p.git", "", "4", "1", "", "n", "2", "n", "n", "n", "1", "", "n"]
        with self.assertRaises(SystemExit):
            self.run_wizard(answers, ["sk-ant-x"])

    def test_requires_interactive_terminal(self):
        with mock.patch("sys.stdin.isatty", return_value=False):
            with self.assertRaises(SystemExit):
                wizard.run_wizard()


class RepoCheckTest(unittest.TestCase):
    def test_check_repo_against_local_repo(self):
        from test_pipeline import commit, git
        origin = Path(tempfile.mkdtemp()) / "o"
        origin.mkdir()
        git(origin, "init", "-q", "-b", "main")
        commit(origin, {"a.txt": "x\n"}, "c1")
        self.assertTrue(wizard.check_repo(str(origin), "main")[0])
        good, msg = wizard.check_repo(str(origin), "nao-existe")
        self.assertFalse(good)
        self.assertIn("não existe", msg)


if __name__ == "__main__":
    unittest.main()
