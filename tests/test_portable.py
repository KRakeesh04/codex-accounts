import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_accounts.cli import parser
from codex_accounts.manager import import_account
from codex_accounts.picker import pick
from codex_accounts.state import Store


class PortableTests(unittest.TestCase):
    def test_import_parser_accepts_default_and_explicit_home(self):
        arguments = parser().parse_args(["import", "account"])
        self.assertEqual(arguments.command, "import")
        self.assertEqual(arguments.subject, "account")
        self.assertIsNone(arguments.home)
        arguments = parser().parse_args(["import", "account", "--home", "saved-home"])
        self.assertEqual(arguments.home, "saved-home")

    def test_import_uses_default_home_without_codex_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = Store(root / "registry")
            for environment in ({}, {"CODEX_HOME": ""}):
                with (
                    self.subTest(environment=environment),
                    patch.dict(os.environ, environment, clear=True),
                    patch("codex_accounts.manager.Path.home", return_value=root),
                    patch("codex_accounts.manager.add_account", return_value=0) as add,
                ):
                    self.assertEqual(import_account(store), 0)
                    add.assert_called_once_with(
                        store, existing_home=str(root / ".codex")
                    )

    def test_parser_keeps_the_singular_select_account_command(self):
        arguments = parser().parse_args(["select", "account"])

        self.assertEqual(arguments.command, "select")
        self.assertEqual(arguments.subject, "account")
        self.assertTrue(arguments.run)

    def test_numbered_picker_works_without_posix_terminal_features(self):
        output = io.StringIO()
        with (
            patch.dict(os.environ, {"TERM": "dumb"}),
            patch("sys.stdin", io.StringIO("2\n")),
            patch("sys.stdout", output),
        ):
            selected = pick(
                [("first", "first@example.com"), ("second", "second@example.com")]
            )

        self.assertEqual(selected, "second")
        self.assertIn("second@example.com", output.getvalue())

    def test_store_writes_a_valid_account_registry_on_every_platform(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "account-home"
            home.mkdir()
            store = Store(root / "registry")
            with store.edit() as state:
                state["accounts"]["account-1"] = {
                    "home": str(home),
                    "email": "account@example.com",
                    "plan": "pro",
                    "kind": "chatgpt",
                }
                state["selected"] = "account-1"

            saved = store.read()
            self.assertEqual(saved["selected"], "account-1")
            self.assertEqual(
                saved["accounts"]["account-1"]["email"], "account@example.com"
            )


if __name__ == "__main__":
    unittest.main()
