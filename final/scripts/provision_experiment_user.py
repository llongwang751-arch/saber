"""Provision a server-trusted account for tenant-scoped online experiments.

Passwords are read interactively and never printed or accepted as a command
line argument.  Experiment roles remain controlled by the configured username
allowlists so this command cannot create a role that login later removes.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import bcrypt

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from internal.application.auth import AuthService  # noqa: E402
from internal.application.store import ApplicationStore, NotFoundError  # noqa: E402


def provision_user(
    store: ApplicationStore,
    *,
    username: str,
    tenant_id: str,
    experiment_eligible: bool,
    create: bool,
    password_reader: Callable[[str], str] = getpass.getpass,
) -> dict[str, Any]:
    auth = AuthService(store)
    normalized_username = str(username or "").strip()
    roles = auth.configured_roles(normalized_username)
    try:
        current = store.find_user_by_username(normalized_username)
        created = False
    except NotFoundError:
        if not create:
            raise ValueError("用户不存在；如需新建，请显式添加 --create")
        first = password_reader("新账号密码：")
        second = password_reader("再次输入密码：")
        if first != second:
            raise ValueError("两次输入的密码不一致")
        _, validated_password = auth.validate_credentials(
            normalized_username, first
        )
        digest = bcrypt.hashpw(
            validated_password.encode("utf-8"), bcrypt.gensalt(rounds=12)
        ).decode("ascii")
        current = store.create_user(
            normalized_username,
            digest,
            tenant_id=tenant_id,
            roles=roles,
            identity_provenance="operator_provisioned",
            experiment_eligible=experiment_eligible,
        )
        created = True
    if not created:
        current = store.set_user_identity(
            current["id"],
            tenant_id=tenant_id,
            roles=roles,
            identity_provenance="operator_provisioned",
            experiment_eligible=experiment_eligible,
        )
    return {
        "created": created,
        "user_id": current["id"],
        "username": current["username"],
        "tenant_id": current["tenant_id"],
        "roles": current["roles"],
        "identity_provenance": current["identity_provenance"],
        "experiment_eligible": current["experiment_eligible"],
        "created_at": current["created_at"].isoformat(),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="后台预置租户账号，并显式设置真实 A/B 实验资格"
    )
    parser.add_argument("--username", required=True, help="账号用户名")
    parser.add_argument("--tenant-id", required=True, help="所属租户")
    parser.add_argument(
        "--database-url",
        default=None,
        help="应用数据库 DSN；省略时使用 AGI_EVAL_DATABASE_URL",
    )
    parser.add_argument(
        "--create",
        action="store_true",
        help="账号不存在时交互式创建；密码不会出现在命令行或输出中",
    )
    parser.add_argument(
        "--experiment-eligible",
        action="store_true",
        help="允许该普通业务账号计入 production_authenticated 实验",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = ApplicationStore(args.database_url)
    try:
        result = provision_user(
            store,
            username=args.username,
            tenant_id=args.tenant_id,
            experiment_eligible=bool(args.experiment_eligible),
            create=bool(args.create),
        )
    finally:
        store.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
