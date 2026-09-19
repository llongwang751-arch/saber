# preference — 用户偏好独立模块（与 main 分支 internal/domain/memory/preference 对齐）。
#
# 与 main 分支 Go Preference 的差异：
#   - Python 版仍然持有 ``user_id`` 与 ``inf``，因为现有 repo 接口按 user_id 维度
#     做读写；main 分支由更高层装配 user_id，这里出于历史原因保持现状不动。
#   - ExtractAndSave 规则、BuildContext 输出格式、对外方法签名严格对齐。
import logging
import threading
from typing import Dict, Optional, Tuple

from internal.infra.infra import Infrastructure

logger = logging.getLogger(__name__)


class Preference:
    """用户偏好管理。线程安全：data 读写均走 RLock。"""

    def __init__(self, user_id: str, inf: Infrastructure):
        self.user_id = user_id
        self.inf = inf
        self.preferences: Dict[str, str] = {}
        self._lock = threading.RLock()
        self.load_from_storage()

    @property
    def data(self) -> Dict[str, str]:
        return self.preferences

    def load_from_storage(self) -> None:
        loaded = self.inf.repo.preference.load(self.user_id) or {}
        with self._lock:
            self.preferences = dict(loaded)
        logger.info("✅ 加载用户 %s 的偏好: %s", self.user_id, loaded)

    def set(self, key: str, value: str) -> None:
        if not key or value is None:
            return
        with self._lock:
            self.inf.repo.preference.save(self.user_id, key, value)
            self.preferences[key] = value

    def save_batch(self, kvs: Dict[str, str]) -> None:
        for k, v in (kvs or {}).items():
            self.set(str(k), str(v))

    def get(self, key: str, default: str = "") -> str:
        with self._lock:
            return self.preferences.get(key, default)

    def get_all(self) -> Dict[str, str]:
        with self._lock:
            return dict(self.preferences)

    def snapshot(self) -> Dict[str, str]:
        return self.get_all()

    # ─── main 分支对齐 ─────────────────────────────────────────────────────

    def extract_and_save(self, msg: str) -> Tuple[str, str, bool]:
        """从用户输入提取偏好与画像并落库（融合 TencentDB-Agent-Memory 槽位抽取思想）。

        支持正面喜好、负面限制/忌口（否定句不被反向误读）、身份职业、输出格式等，
        同时完全兼容原有 key/value 对齐协议。
        """
        if not msg:
            return "", "", False

        from .slot_extractor import SlotExtractor
        slots = SlotExtractor.extract_slots(msg)
        if not slots:
            return "", "", False

        # 将所有识别到的槽位存入偏好中
        for slot in slots:
            self.set(slot.key, slot.value)

        primary = slots[0]
        return primary.key, primary.value, True

    def build_context(self) -> str:
        """渲染【用户偏好】块；空数据返回空串。

        输出格式：首行 "【用户偏好】"，随后每行 "key: value"。
        """
        snap = self.snapshot()
        if not snap:
            return ""
        lines = [f"{k}: {v}" for k, v in snap.items()]
        return "【用户偏好】\n" + "\n".join(lines)


__all__ = ["Preference"]
