# snapshot — 任务快照仓储（Postgres 实现）。
import json
import logging
from typing import List

from internal.platform.postgres import PostgresClient

logger = logging.getLogger(__name__)


class PGRepo:
    """Postgres 实现；client 不可用时降级为空操作。"""

    def __init__(self, client: PostgresClient):
        self.client = client

    def get(self, task_id: str, user_id: str = 'default_user'):
        row = self.client.query_one('SELECT state FROM task_snapshots WHERE task_id=%s AND user_id=%s',
                                    (task_id, user_id))
        if not row:
            return None
        return json.loads(row[0]) if isinstance(row[0], str) else row[0]

    # upsert 任务快照（同一 task_id 多次保存覆盖最新状态）
    def save(self, task_id: str, state_json: bytes, user_id: str = "default_user") -> None:
        if self.client is None or not self.client.is_real():
            raise RuntimeError("Snapshot persistence is unavailable")
        # JSONB 字段接受 bytes / str
        state_param = state_json
        if isinstance(state_param, (bytes, bytearray)):
            try:
                state_param = bytes(state_param).decode("utf-8")
            except Exception:
                state_param = "{}"
        try:
            affected = self.client.exec(
                "INSERT INTO task_snapshots (task_id, user_id, state) VALUES (%s, %s, %s) "
                "ON CONFLICT (task_id) DO UPDATE SET state = EXCLUDED.state, created_at = NOW() "
                "WHERE task_snapshots.user_id = EXCLUDED.user_id",
                (task_id, user_id, state_param),
            )
            if affected != 1:
                raise RuntimeError("Snapshot persistence failed or task belongs to another user")
        except Exception as e:
            logger.warning("⚠️  快照保存到 PG 失败: %s", e)
            raise

    # 列出最近 limit 条任务快照（按 created_at 倒序）
    def list(self, limit: int = 50, user_id: str = "default_user") -> List[dict]:
        if self.client is None or not self.client.is_real():
            return []
        try:
            rows = self.client.query(
                "SELECT task_id, state, created_at FROM task_snapshots "
                "WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
                (user_id, limit),
            )
        except Exception as e:
            logger.warning("⚠️  快照列表加载失败: %s", e)
            return []
        out: List[dict] = []
        for task_id, state, created_at in rows:
            if isinstance(state, str):
                try:
                    state = json.loads(state)
                except Exception:
                    pass
            out.append({
                "task_id": task_id,
                "state": state,
                "created_at": created_at.isoformat() if created_at is not None and hasattr(created_at, "isoformat") else None,
            })
        return out
