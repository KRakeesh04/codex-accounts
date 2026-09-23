"""Exercise installed wheels through real processes without live credentials."""

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class Installation:
    def __init__(self, root: Path, wheels: Path, reports: Path):
        self.root = root
        self.reports = reports
        self.bin = root / "installed-bin"
        self.backend_bin = root / "backend bin"
        self.tools = root / "tools"
        self.environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("UV_", "CODEX_", "OPENAI_", "E2E_"))
            and key not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")
        }
        self.environment.update(
            UV_TOOL_DIR=str(self.tools),
            UV_CACHE_DIR=str(root / "cache"),
            UV_PYTHON_DOWNLOADS="never",
            PYTHONUTF8="1",
            PYTHONNOUSERSITE="1",
        )
        uv = shutil.which("uv")
        if uv is None:
            raise RuntimeError("Install uv before running E2E tests")
        for pattern, destination in (
            ("codex_accounts-*.whl", self.bin),
            ("codex_accounts_e2e_backend-*.whl", self.backend_bin),
        ):
            matches = list(wheels.glob(pattern))
            if len(matches) != 1:
                raise RuntimeError(f"Expected exactly one wheel matching {pattern}")
            self.run(
                [
                    uv,
                    "tool",
                    "install",
                    "--offline",
                    "--python",
                    sys.executable,
                    str(matches[0]),
                ],
                env={**self.environment, "UV_TOOL_BIN_DIR": str(destination)},
                cwd=root,
                timeout=120,
            ).check_returncode()
        self.backend = self.backend_bin / ("codex.exe" if os.name == "nt" else "codex")
        self.backends = {"native": self.backend}
        if os.name == "nt":
            for extension in ("cmd", "bat"):
                directory = root / f"{extension} backend"
                directory.mkdir()
                shim = directory / f"codex.{extension}"
                shim.write_text(
                    f'@echo off\n"{self.backend}" %*\nexit /b %errorlevel%\n',
                    encoding="utf-8",
                )
                self.backends[extension] = shim

    def run(self, command: list[str], **kwargs) -> subprocess.CompletedProcess:
        with (self.reports / "commands.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps({"command": command}) + "\n")
            log.flush()
            try:
                result = subprocess.run(
                    command, capture_output=True, text=True, encoding="utf-8", **kwargs
                )
            except subprocess.TimeoutExpired:
                log.write(json.dumps({"timeout": True}) + "\n")
                raise
            log.write(
                json.dumps(
                    {
                        "returncode": result.returncode,
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                    }
                )
                + "\n"
            )
        return result


class ImportE2E(unittest.TestCase):
    installation: Installation
    backend_kind: str

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=self.installation.root)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.user = self.root / "user é space"
        self.user.mkdir()
        self.home = self.user / ".codex"
        self.make_home(self.home, "first@example.com")
        self.registry = self.root / "registry"
        self.shared = self.root / "shared data"
        self.environment = {
            **self.installation.environment,
            "HOME": str(self.user),
            "USERPROFILE": str(self.user),
            "LOCALAPPDATA": str(self.user / "AppData/Local"),
            "APPDATA": str(self.user / "AppData/Roaming"),
            "XDG_DATA_HOME": str(self.user / ".local/share"),
            "XDG_CONFIG_HOME": str(self.user / ".config"),
            "CODEX_HOME": str(self.home),
            "CODEX_ACCOUNTS_HOME": str(self.registry),
            "CODEX_ACCOUNTS_HISTORY_HOME": str(self.shared),
            "CODEX_ACCOUNTS_CODEX_BIN": str(
                self.installation.backends[self.backend_kind]
            ),
            "E2E_ROOT": str(self.root),
            "OPENAI_API_KEY": "FAKE-SECRET",
            "CODEX_API_KEY": "FAKE-SECRET",
            "CODEX_ACCESS_TOKEN": "FAKE-SECRET",
            "PATH": os.pathsep.join(
                [
                    str(self.installation.bin),
                    str(self.installation.backends[self.backend_kind].parent),
                    self.installation.environment.get("PATH", ""),
                ]
            ),
        }

    def make_home(self, home: Path, email: str) -> None:
        home.mkdir(parents=True)
        (home / "auth.json").write_text(
            json.dumps(
                {
                    "type": "chatgpt",
                    "email": email,
                    "planType": "plus",
                    "accessToken": "FAKE-SECRET",
                }
            ),
            encoding="utf-8",
        )

    def invoke(
        self, *arguments: str, command="codex", code=0, env=None, input_text=None
    ):
        environment = self.environment if env is None else env
        executable = shutil.which(command, path=environment["PATH"])
        self.assertIsNotNone(executable)
        self.assertEqual(Path(executable).parent, self.installation.bin)
        result = self.installation.run(
            [executable, *arguments],
            env=environment,
            cwd=self.root,
            input=input_text,
            timeout=40,
        )
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertNotIn("FAKE-SECRET", result.stdout + result.stderr)
        return result

    def state(self) -> dict:
        return json.loads((self.registry / "accounts.json").read_text(encoding="utf-8"))

    def events(self) -> list[dict]:
        path = self.root / "events.jsonl"
        return (
            [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            if path.exists()
            else []
        )

    def tearDown(self):
        for event in self.events():
            if "args" in event:
                self.assertNotIn(event["args"][:1], (["login"], ["logout"]))
                self.assertEqual(event["auth_overrides"], [])
            elif event["rpc"]["method"] == "account/read":
                self.assertEqual(event["rpc"]["params"], {"refreshToken": False})

    def test_import_lists_identity_without_copying_credentials(self):
        before = (self.home / "auth.json").read_bytes()
        self.invoke("import", "account")
        listing = json.loads(
            self.invoke("list", "accounts", "--json", command="codex-accounts").stdout
        )
        self.assertIsNone(listing["selected"])
        self.assertEqual(len(listing["accounts"]), 1)
        record = listing["accounts"][0]
        self.assertEqual(record["email"], "first@example.com")
        self.assertEqual(record["plan"], "plus")
        self.assertEqual(Path(record["home"]), self.home)
        self.assertEqual((self.home / "auth.json").read_bytes(), before)
        self.assertFalse((self.registry / "homes").exists())
        self.assertNotIn("FAKE-SECRET", (self.registry / "accounts.json").read_text())
        self.assertTrue(
            all(
                event["args"] == ["app-server"]
                for event in self.events()
                if "args" in event
            )
        )

    def test_default_user_home(self):
        environment = dict(self.environment)
        environment.pop("CODEX_HOME")
        self.invoke("import", "account", env=environment)
        self.assertEqual(
            Path(next(iter(self.state()["accounts"].values()))["home"]), self.home
        )

    def test_explicit_home_overrides_environment_and_preserves_selection(self):
        self.invoke("import", "account")
        self.invoke("select", "account", "first@example.com", "--no-run")
        selected = self.state()["selected"]
        second = self.root / "second é account"
        self.make_home(second, "second@example.com")
        self.invoke(
            "import", "account", "--home", str(second), command="codex-accounts"
        )
        self.assertEqual(self.state()["selected"], selected)
        self.assertEqual(len(self.state()["accounts"]), 2)
        launched = json.loads(
            self.invoke("--account", "second@example.com", "--e2e-echo").stdout
        )
        self.assertEqual(Path(launched["home"]), second)
        self.assertEqual(self.state()["selected"], selected)

    def test_duplicate_path_is_rejected_without_changing_registry(self):
        self.invoke("import", "account")
        before = (self.registry / "accounts.json").read_bytes()
        result = self.invoke(
            "import", "account", "--home", str(self.home / ".." / ".codex"), code=2
        )
        self.assertIn("already in the account list", result.stderr)
        self.assertEqual((self.registry / "accounts.json").read_bytes(), before)

    def test_missing_and_empty_home_do_not_create_accounts(self):
        for home in (str(self.root / "missing"), ""):
            with self.subTest(home=home):
                self.invoke("import", "account", "--home", home, code=2)
                self.assertFalse((self.registry / "accounts.json").exists())
        self.assertEqual(self.events(), [])

    def test_signed_out_home_does_not_start_login(self):
        (self.home / "auth.json").unlink()
        result = self.invoke("import", "account", code=2)
        self.assertIn("no saved login", result.stderr)
        self.assertFalse((self.registry / "accounts.json").exists())

    def test_metadata_failures_preserve_registry_and_credentials(self):
        self.invoke("import", "account")
        before = (self.registry / "accounts.json").read_bytes()
        second = self.root / "unverified"
        self.make_home(second, "second@example.com")
        credential = (second / "auth.json").read_bytes()
        for mode in ("error", "malformed", "exit"):
            with self.subTest(mode=mode):
                result = self.invoke(
                    "import",
                    "account",
                    "--home",
                    str(second),
                    code=2,
                    env={**self.environment, "E2E_METADATA": mode},
                )
                self.assertIn("Could not verify", result.stderr)
                self.assertEqual((self.registry / "accounts.json").read_bytes(), before)
                self.assertEqual((second / "auth.json").read_bytes(), credential)

    def test_selection_launch_stdin_and_exit_status(self):
        self.invoke("import", "account")
        self.invoke("select", "account", "first@example.com", "--no-run")
        launched = json.loads(self.invoke("--e2e-echo").stdout)
        self.assertEqual(Path(launched["home"]), self.home)
        self.assertEqual(Path(launched["sqlite_home"]), self.shared)
        self.assertEqual(
            self.invoke("--e2e-stdin", input_text="hello é\n").stdout, "hello é\n"
        )
        self.invoke("--e2e-exit", code=37)
        self.invoke("select", "account", "first@example.com")
        self.assertEqual(self.events()[-1]["args"], [])

    def test_picker_reads_quota_and_selects_without_launching(self):
        self.invoke("import", "account")
        result = self.invoke("select", "account", "--no-run", input_text="1\n")
        self.assertIn("5h: 75% left", result.stdout)
        self.assertIsNotNone(self.state()["selected"])

    def test_shared_files_and_removal_retain_credentials(self):
        skills = self.home / "skills"
        skills.mkdir()
        (skills / "example.txt").write_text("skill", encoding="utf-8")
        sessions = self.home / "sessions"
        sessions.mkdir()
        (sessions / "example.jsonl").write_text("{}\n", encoding="utf-8")
        self.invoke("import", "account")
        self.assertEqual((self.shared / "skills/example.txt").read_text(), "skill")
        self.assertEqual((self.shared / "sessions/example.jsonl").read_text(), "{}\n")
        (self.shared / "skills/another.txt").write_text("new", encoding="utf-8")
        self.assertEqual((self.home / "skills/another.txt").read_text(), "new")
        self.invoke("remove", "account", "first@example.com")
        self.assertEqual(self.state()["accounts"], {})
        self.assertTrue((self.home / "auth.json").is_file())

    def test_backend_discovery_skips_installed_wrapper(self):
        environment = dict(self.environment)
        environment.pop("CODEX_ACCOUNTS_CODEX_BIN")
        self.invoke("import", "account", env=environment)
        self.assertEqual(len(self.state()["accounts"]), 1)

    def test_package_is_loaded_from_uv_not_the_checkout(self):
        environment = self.installation.tools / "codex-accounts"
        interpreter = environment / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python"
        )
        result = self.installation.run(
            [
                str(interpreter),
                "-I",
                "-c",
                "import codex_accounts; print(codex_accounts.__file__)",
            ],
            env=self.environment,
            cwd=self.root,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            Path(result.stdout.strip()).resolve().is_relative_to(environment)
        )

    def test_arguments_with_spaces_and_unicode_survive_launch(self):
        self.invoke("import", "account")
        self.invoke("select", "account", "first@example.com", "--no-run")
        arguments = [
            "exec",
            "review this é project",
            "--cd",
            str(self.root / "project space"),
        ]
        result = self.invoke(*arguments)
        self.assertEqual(json.loads(result.stdout)["args"], arguments)

    def test_installed_commands_work_from_native_shells(self):
        self.invoke("import", "account")
        if os.name == "nt":
            shells = [
                [os.environ["COMSPEC"], "/d", "/c"],
                ["pwsh", "-NoProfile", "-NonInteractive", "-Command"],
            ]
        else:
            shells = [["bash", "--noprofile", "--norc", "-c"]]
            if sys.platform == "darwin":
                shells.append(["zsh", "-f", "-c"])
        for shell in shells:
            for command in ("codex", "codex-accounts"):
                with self.subTest(shell=shell[0], command=command):
                    executable = shutil.which(shell[0])
                    self.assertIsNotNone(
                        executable, f"Required shell missing: {shell[0]}"
                    )
                    script = f"{command} list accounts --json"
                    if shell[0] == "pwsh":
                        script += "; exit $LASTEXITCODE"
                    result = self.installation.run(
                        [executable, *shell[1:], script],
                        env=self.environment,
                        cwd=self.root,
                        timeout=40,
                    )
                    self.assertEqual(
                        result.returncode, 0, result.stdout + result.stderr
                    )
                    self.assertEqual(
                        json.loads(result.stdout)["accounts"][0]["email"],
                        "first@example.com",
                    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel-dir", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    arguments = parser.parse_args()
    reports = arguments.report_dir.resolve()
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "platform.json").write_text(
        json.dumps(
            {
                "platform": platform.platform(),
                "python": sys.version,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    with tempfile.TemporaryDirectory(prefix="codex-e2e-") as temporary:
        installation = Installation(
            Path(temporary).resolve(), arguments.wheel_dir.resolve(), reports
        )
        suite = unittest.TestSuite()
        for backend in installation.backends:
            case = type(
                f"{backend.title()}ImportE2E",
                (ImportE2E,),
                {
                    "installation": installation,
                    "backend_kind": backend,
                },
            )
            suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(case))
        with (reports / "results.txt").open("w", encoding="utf-8") as report:
            result = unittest.TextTestRunner(stream=report, verbosity=2).run(suite)
        print((reports / "results.txt").read_text(encoding="utf-8"))
        return (
            0
            if result.wasSuccessful() and not result.skipped and result.testsRun
            else 1
        )


if __name__ == "__main__":
    raise SystemExit(main())
