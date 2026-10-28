import tempfile
import unittest
from pathlib import Path
from unittest import mock

import anthropic
import httpx

from reviewer import errors, main, ui
from reviewer.gitrepo import GitError

ui.enabled = False
REQ = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
# Chaves FALSAS, montadas em partes para o código-fonte não ter nada com formato de chave real
# (evita alarme falso de scanners de segredos, como a proteção de push do GitHub).
TG_TOKEN = "123456789:" + "AA" + "EhBP0abcdefghijklmnopqrstuvwxyz12"
ANT_KEY = "sk-" + "ant-" + "api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"


def api_err(cls, status, message="x"):
    return cls(message, response=httpx.Response(status, request=REQ), body=None)


class ExplainTest(unittest.TestCase):
    def title(self, exc):
        return errors.explain(exc)[0]

    def test_anthropic_errors(self):
        self.assertIn("recusada", self.title(api_err(anthropic.AuthenticationError, 401)))
        self.assertIn("429", self.title(api_err(anthropic.RateLimitError, 429)))
        self.assertIn("instável", self.title(api_err(anthropic.InternalServerError, 500)))
        self.assertIn("conexão", self.title(anthropic.APIConnectionError(request=REQ)))
        self.assertIn("demorou", self.title(anthropic.APITimeoutError(request=REQ)))

    def test_credit_balance_gets_specific_message(self):
        exc = api_err(anthropic.BadRequestError, 400, "Your credit balance is too low to access the API")
        title, _detail, hint = errors.explain(exc)
        self.assertIn("Saldo", title)
        self.assertIn("Billing", hint)

    def test_other_bad_request_shows_detail(self):
        title, detail, _ = errors.explain(api_err(anthropic.BadRequestError, 400, "prompt is too long"))
        self.assertIn("400", title)
        self.assertIn("prompt is too long", detail)

    def test_git_errors(self):
        net = GitError("git fetch falhou: fatal: unable to access 'https://x': Failed to connect to github.com")
        self.assertIn("rede", self.title(net))
        self.assertIn("recusou", self.title(GitError("git fetch falhou: fatal: Authentication failed for 'x'")))
        self.assertIn("branch", self.title(GitError("git fetch falhou: fatal: couldn't find remote ref main")))

    def test_telegram_and_slack_hints(self):
        _, _, hint = errors.explain(RuntimeError("Telegram sendMessage: Forbidden: bot was kicked from the group"))
        self.assertIn("grupo", hint)
        _, _, hint = errors.explain(RuntimeError("Slack: not_in_channel"))
        self.assertIn("/invite", hint)

    def test_truncated_response_and_generic_runtime(self):
        self.assertIn("cortada", self.title(RuntimeError("Resposta cortada em max_tokens (revisão)")))
        self.assertIn("não terminou", self.title(RuntimeError("Etapa revisão terminou sem entregar o resultado")))

    def test_unknown_error_mentions_type_and_debug(self):
        title, _, hint = errors.explain(ZeroDivisionError("boom"))
        self.assertIn("ZeroDivisionError", title)
        self.assertIn("--debug", hint)


class RedactTest(unittest.TestCase):
    def test_secrets_are_hidden(self):
        raw = (f"GET https://api.telegram.org/bot{TG_TOKEN}/getMe key={ANT_KEY} "
               "url=https://user:pw123@github.com/x Authorization: Bearer abc.def.ghi " + "xo" + "xb-111-222-aaa")
        out = errors.redact(raw)
        for secret in (TG_TOKEN, ANT_KEY, "pw123", "abc.def.ghi", "xoxb-111"):
            self.assertNotIn(secret, out)

    def test_explain_never_leaks_secrets_in_detail(self):
        exc = GitError(f"git clone falhou: fatal: could not read from 'https://user:pw123@github.com/x'")
        _, detail, _ = errors.explain(exc)
        self.assertNotIn("pw123", detail)
        _, detail, _ = errors.explain(RuntimeError(f"Telegram sendMessage: https://api.telegram.org/bot{TG_TOKEN}/x"))
        self.assertNotIn(TG_TOKEN, detail)

    def test_error_log_file_is_redacted(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            raise RuntimeError(f"falhou com {ANT_KEY} e {TG_TOKEN}")
        except RuntimeError as exc:
            with mock.patch.object(main, "ROOT", tmp):
                path = main._save_error_log(exc)
        content = path.read_text(encoding="utf-8")
        self.assertNotIn(ANT_KEY, content)
        self.assertNotIn(TG_TOKEN, content)
        self.assertIn("RuntimeError", content)


class UiSafetyTest(unittest.TestCase):
    def test_wait_helpers_are_noops_when_disabled(self):
        ui.wait_start("x")
        ui.wait_progress(10)
        ui.wait_label("y")
        ui.wait_stop()

    def test_error_prints_even_when_quiet(self):
        with mock.patch("sys.stderr") as err:
            ui.error("Título", "detalhe", "faça isto")
        written = "".join(c.args[0] for c in err.write.call_args_list)
        self.assertIn("Título", written)
        self.assertIn("faça isto", written)


if __name__ == "__main__":
    unittest.main()
