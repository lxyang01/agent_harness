from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from billguard.auth import AuthError, UserStore
from billguard.users import main


class UsersCliTests(unittest.TestCase):
    def run_cli(self, root: Path, *argv: str, stdin: str = "") -> int:
        with mock.patch("sys.stdin", io.StringIO(stdin)):
            return main(["--data-dir", str(root), *argv])

    def test_add_list_set_role_reset_disable_enable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(0, self.run_cli(root, "add", "root", "--role", "admin", "--password-stdin",
                                             stdin="root-pass-1234\nroot-pass-1234\n"))
            store = UserStore(root / "billguard" / "auth")
            self.assertEqual(1, store.count())
            self.assertEqual(0, self.run_cli(root, "add", "bob", "--role", "viewer", "--password-stdin",
                                             stdin="bob-pass-1234\n"))
            self.assertEqual(0, self.run_cli(root, "list"))  # list 成功返回 0
            self.assertEqual(0, self.run_cli(root, "set-role", "bob", "--role", "approver"))
            self.assertEqual("approver", store.get("bob").role)
            self.assertEqual(0, self.run_cli(root, "reset-password", "bob", "--password-stdin",
                                             stdin="new-pass-12345\n"))
            self.assertEqual("viewer", UserStore(root / "billguard" / "auth").set_role("bob", "viewer").role)
            self.assertEqual(0, self.run_cli(root, "disable", "bob"))
            self.assertTrue(store.get("bob").disabled)
            self.assertEqual(0, self.run_cli(root, "enable", "bob"))
            self.assertFalse(store.get("bob").disabled)

    def test_short_password_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(1, self.run_cli(root, "add", "bob", "--role", "viewer", "--password-stdin",
                                             stdin="short\nshort\n"))
            self.assertEqual(0, UserStore(root / "billguard" / "auth").count())

    def test_last_admin_guard(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.run_cli(root, "add", "root", "--role", "admin", "--password-stdin",
                         stdin="root-pass-1234\nroot-pass-1234\n")
            self.assertEqual(1, self.run_cli(root, "disable", "root"))
