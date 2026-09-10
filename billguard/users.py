from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from .auth import ROLES, UserStore


def _read_password(args: argparse.Namespace, confirm: bool) -> str:
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    first = getpass.getpass("密码: ")
    if confirm and first != getpass.getpass("再次输入密码: "):
        raise SystemExit("两次输入的密码不一致")
    return first


def _store(args: argparse.Namespace) -> UserStore:
    # 与 web.serve() 保持同一子树:<data-dir>/billguard/auth
    return UserStore(Path(args.data_dir) / "billguard" / "auth")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BillGuard 用户管理")
    parser.add_argument("--data-dir", default=".sessions", help="数据目录(认证库位于 billguard/auth 子目录)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    add = subparsers.add_parser("add", help="创建用户")
    add.add_argument("username")
    add.add_argument("--role", choices=ROLES, required=True)
    add.add_argument("--password-stdin", action="store_true", help="从 stdin 读取密码")

    subparsers.add_parser("list", help="列出用户")

    role = subparsers.add_parser("set-role", help="修改角色")
    role.add_argument("username")
    role.add_argument("--role", choices=ROLES, required=True)

    reset = subparsers.add_parser("reset-password", help="重置密码")
    reset.add_argument("username")
    reset.add_argument("--password-stdin", action="store_true")

    disable = subparsers.add_parser("disable", help="禁用用户")
    disable.add_argument("username")
    enable = subparsers.add_parser("enable", help="启用用户")
    enable.add_argument("username")

    args = parser.parse_args(argv)
    store = _store(args)
    try:
        if args.command == "add":
            store.create(args.username, _read_password(args, confirm=True), args.role)
            print(f"已创建用户 {args.username}({args.role})")
        elif args.command == "list":
            users = store.list()
            for user in users:
                state = "已禁用" if user.disabled else "启用中"
                print(f"{user.username}\t{user.role}\t{state}")
            return 0
        elif args.command == "set-role":
            store.set_role(args.username, args.role)
            print(f"{args.username} 的角色已改为 {args.role}")
        elif args.command == "reset-password":
            store.reset_password(args.username, _read_password(args, confirm=False))
            print(f"{args.username} 的密码已重置")
        elif args.command == "disable":
            store.set_disabled(args.username, True)
            print(f"{args.username} 已禁用")
        elif args.command == "enable":
            store.set_disabled(args.username, False)
            print(f"{args.username} 已启用")
    except ValueError as exc:  # AuthError 继承 ValueError
        print(f"错误:{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
