from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from billguard.auth import UserStore
from billguard.storage_pg import PGUserStore
from billguard.users import main

from tests.conftest import PG_DSN, StoreTestCase


class UsersCliTests(StoreTestCase):
    """users CLI 走 PG 分支(BILLGUARD_PG_DSN 已设置 → PGUserStore)。"""

    def setUp(self) -> None:
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def run_cli(self, root: Path, *argv: str, stdin: str = "") -> int:
        env = {"BILLGUARD_PG_DSN": PG_DSN}
        with mock.patch.dict("os.environ", env), \
                mock.patch("sys.stdin", io.StringIO(stdin)):
            # --data-dir 仍传入(PG 分支忽略之,保持 CLI 参数面兼容)
            return main(["--data-dir", str(root), *argv])

    def store(self) -> PGUserStore:
        return PGUserStore(self.pool)

    def test_add_list_set_role_reset_disable_enable(self):
        root = Path(self.temp.name)
        self.assertEqual(0, self.run_cli(root, "add", "root", "--role", "admin", "--password-stdin",
                                         stdin="root-pass-1234\nroot-pass-1234\n"))
        store = self.store()
        self.assertEqual(1, store.count())
        self.assertEqual(0, self.run_cli(root, "add", "bob", "--role", "user", "--password-stdin",
                                         stdin="bob-pass-1234\n"))
        self.assertEqual(0, self.run_cli(root, "list"))  # list 成功返回 0
        self.assertEqual(0, self.run_cli(root, "set-role", "bob", "--role", "user"))
        self.assertEqual("user", store.get("bob").role)
        self.assertEqual(0, self.run_cli(root, "reset-password", "bob", "--password-stdin",
                                         stdin="new-pass-12345\n"))
        self.assertEqual("user", self.store().set_role("bob", "user").role)
        self.assertEqual(0, self.run_cli(root, "disable", "bob"))
        self.assertTrue(store.get("bob").disabled)
        self.assertEqual(0, self.run_cli(root, "enable", "bob"))
        self.assertFalse(store.get("bob").disabled)

    def test_short_password_rejected(self):
        root = Path(self.temp.name)
        self.assertEqual(1, self.run_cli(root, "add", "bob", "--role", "user", "--password-stdin",
                                         stdin="short\nshort\n"))
        self.assertEqual(0, self.store().count())

    def test_last_admin_guard(self):
        root = Path(self.temp.name)
        self.run_cli(root, "add", "root", "--role", "admin", "--password-stdin",
                     stdin="root-pass-1234\nroot-pass-1234\n")
        self.assertEqual(1, self.run_cli(root, "disable", "root"))


class UsersCliFallbackTests(unittest.TestCase):
    """F5 回退分支(BILLGUARD_PG_DSN 未设):文件 UserStore,<data-dir> 生效。
    修复轮 1 补:主用例全量切 PG 后该分支失去了自动化覆盖。"""

    def run_cli(self, root: Path, *argv: str, stdin: str = "") -> int:
        with mock.patch.dict("os.environ") as env, \
                mock.patch("sys.stdin", io.StringIO(stdin)):
            env.pop("BILLGUARD_PG_DSN", None)
            return main(["--data-dir", str(root), *argv])

    def test_fallback_uses_file_user_store(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(0, self.run_cli(
                root, "add", "root", "--role", "admin", "--password-stdin",
                stdin="root-pass-1234\nroot-pass-1234\n"))
            users = UserStore(root / "billguard" / "auth")
            self.assertEqual(1, users.count())  # 账号落在 --data-dir 的文件后端
            self.assertEqual(0, self.run_cli(
                root, "reset-password", "root", "--password-stdin",
                stdin="new-pass-12345\n"))
            self.assertEqual("admin", users.verify("root", "new-pass-12345").role)
