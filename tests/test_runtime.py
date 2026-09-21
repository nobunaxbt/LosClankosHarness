import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from jev_city import commands
from jev_city.cli import main
from jev_city.control import Control, submit
from jev_city.settings import Settings
from jev_city.storage import Store
from jev_city.transport import API


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.control = Control(self.store)
        self.api = API(Settings(self.store.root), self.store, self.control)

    def test_private_state_and_single_controller_lock(self):
        self.store.save("identity.json", {"token": "test-only"})
        self.assertEqual(self.store.path("identity.json").stat().st_mode & 0o777, 0o600)
        with self.store.lock():
            with self.assertRaises(RuntimeError):
                with Store(self.store.root).lock():
                    pass

    def test_stop_prevents_network(self):
        submit(self.store, "stop")
        with patch("urllib.request.urlopen") as net:
            with self.assertRaises(KeyboardInterrupt):
                self.api.request("/api/observe")
            net.assert_not_called()

    def test_recursive_credential_redaction(self):
        value = {
            "token": "private",
            "rows": [{"authorization": "Bearer secret", "wallet": "private-wallet"}],
            "message": "sk-" + "x" * 24,
        }
        output = json.dumps(self.store.redact(value))
        self.assertNotIn("private", output)
        self.assertNotIn("x" * 24, output)

    def test_no_duplicate_registration(self):
        self.store.save("identity.json", {"token": "saved"})
        with patch.object(API, "request") as net:
            with self.assertRaisesRegex(RuntimeError, "saved identity"):
                main(["--state-dir", str(self.store.root), "register", "--name", "Courier"])
            net.assert_not_called()

    def test_registration_does_not_invent_wallet_or_print_token(self):
        response = {
            "token": "private-registration-token",
            "agent": {"name": "Courier"},
            "watchUrl": "/?agent=test",
        }
        stream = io.StringIO()
        with (
            patch.object(API, "request", return_value=response) as net,
            contextlib.redirect_stdout(stream),
        ):
            main(["--state-dir", str(self.store.root), "register", "--name", "Courier"])
        self.assertNotIn("wallet", net.call_args.args[1])
        self.assertNotIn(response["token"], stream.getvalue())

    def test_url_credentials_and_external_request_paths_rejected(self):
        with self.assertRaises(ValueError):
            self.api.request("https://other.example/api/action")
        with self.assertRaises(ValueError):
            Settings(self.store.root, base_url="https://user:secret@example.com")

    def test_model_key_absent_from_process_arguments_and_cleaned_up(self):
        proc = Mock()
        proc.communicate.return_value = (json.dumps({"answers": {}}), "")
        proc.returncode = 0
        proc.poll.return_value = 0
        with (
            patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-provider-key"}),
            patch("subprocess.Popen", return_value=proc) as spawn,
        ):
            self.api.decide({"state": {}})
        self.assertNotIn("test-provider-key", str(spawn.call_args.args))
        self.assertEqual(list(self.store.root.glob(".model-*")), [])

    def test_stop_cancels_inflight_model_process(self):
        proc = Mock()
        proc.returncode = None
        proc.poll.return_value = None

        def communicate(**kw):
            self.control.stop()
            if kw:
                raise subprocess.TimeoutExpired("curl", 0.1)
            return ("", "")

        proc.communicate.side_effect = communicate
        with (
            patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-provider-key"}),
            patch("subprocess.Popen", return_value=proc),
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.api.decide({"state": {}})
        proc.kill.assert_called_once()
        self.assertEqual(list(self.store.root.glob(".model-*")), [])

    def test_status_is_read_only_and_offline(self):
        self.store.save("status.json", {"running": False})
        with patch.object(API, "request") as net, contextlib.redirect_stdout(io.StringIO()):
            main(["--state-dir", str(self.store.root), "status"])
        net.assert_not_called()

    def test_latest_local_command_wins_over_game_messages(self):
        first = submit(self.store, "work")
        last = submit(self.store, "heal")
        q = self.store.root / "orders"
        (q / "public.json").write_text(
            json.dumps(
                {
                    "source": "game-chat",
                    "createdAt": last["createdAt"] + 1,
                    "text": "attack",
                    "id": "public",
                }
            )
        )
        (q / "bad.json").write_text("{")
        self.assertEqual(commands.newest(self.store.root, first["createdAt"])["id"], last["id"])

    def test_current_targets_only_and_new_commands_reset_old_plan(self):
        w = {
            "agents": [
                {"id": "old", "name": "Rival", "state": "dead", "online": False},
                {"id": "new", "name": "Rival", "state": "idle", "online": True},
            ]
        }
        options = commands.choices(w, {})
        self.assertNotIn("hunt_old", options)
        self.assertEqual(options["hunt_new"]["targetId"], "new")
        m = {"plan": "engage", "focusCombat": True}
        commands.apply(m, {"command": "heal"})
        self.assertFalse(m["focusCombat"])
        self.assertEqual(m["humanMode"], "heal")

    def test_hunt_all_is_an_explicit_supported_mode(self):
        selected = commands.choices({}, {})["hunt_all"]
        m = {}
        commands.apply(m, selected)
        self.assertEqual(m["humanMode"], "hunt_all")
