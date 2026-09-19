# neo4j — Neo4j 图数据库平台层薄封装：连接、约束/索引初始化、cypher 执行。
# 失败时降级到不可用（self._driver 为 None），不阻塞应用启动。
import logging
from typing import Any, Dict, List, Optional

from config.config import APIConfig

logger = logging.getLogger(__name__)

try:
    from neo4j import GraphDatabase, basic_auth
    _HAS_NEO4J = True
except ImportError:
    GraphDatabase = None  # type: ignore
    basic_auth = None  # type: ignore
    _HAS_NEO4J = False


# 启动期幂等创建的租户约束/索引。
#
# 先创建复合唯一约束，确认成功后才移除旧的全局 ``entity_name`` 约束。
# 这不会改写或删除节点；没有 user_id 的旧节点会被业务查询 fail-closed 地忽略，
# 需要从主存储重建图投影。
_ENTITY_TENANT_CONSTRAINT = (
    "CREATE CONSTRAINT entity_user_name IF NOT EXISTS "
    "FOR (e:Entity) REQUIRE (e.user_id, e.name) IS UNIQUE"
)
_DROP_LEGACY_ENTITY_CONSTRAINT = "DROP CONSTRAINT entity_name IF EXISTS"
_CONSTRAINTS: List[str] = [
    _ENTITY_TENANT_CONSTRAINT,
    "CREATE INDEX entity_type IF NOT EXISTS FOR (e:Entity) ON (e.type)",
    "CREATE CONSTRAINT memory_user_id IF NOT EXISTS "
    "FOR (m:Memory) REQUIRE (m.user_id, m.mem_id) IS UNIQUE",
]


class Neo4jClient:
    """Neo4j 平台客户端：连接、约束/索引初始化、cypher 执行。"""

    def __init__(self, cfg: APIConfig):
        self.cfg = cfg
        self._driver = None
        self.status: str = "disconnected"
        self._connect()
        if self._driver is not None:
            self.ensure_constraints()

    # ─── 连接 ───
    def _connect(self) -> None:
        if not _HAS_NEO4J:
            logger.warning("⚠️  neo4j Python driver 未安装，知识图谱将降级跳过")
            return
        if not self.cfg.kg_enabled or not self.cfg.neo4j_uri:
            logger.info(
                "ℹ️  Neo4j 未启用（kg_enabled=%s, uri=%r）",
                self.cfg.kg_enabled, self.cfg.neo4j_uri,
            )
            return
        try:
            self._driver = GraphDatabase.driver(
                self.cfg.neo4j_uri,
                auth=basic_auth(self.cfg.neo4j_user, self.cfg.neo4j_password),
            )
            # 连通性验证（driver 内部含超时设置）
            self._driver.verify_connectivity()
            self.status = "connected"
            logger.info("✅ Neo4j 已连接: %s", self.cfg.neo4j_uri)
        except Exception as e:
            logger.warning("⚠️  Neo4j 连接失败: %s（知识图谱将降级跳过）", e)
            try:
                if self._driver is not None:
                    self._driver.close()
            except Exception:
                pass
            self._driver = None

    # ─── 状态判断 ───
    def is_real(self) -> bool:
        return self._driver is not None

    def available(self) -> bool:
        """与 Go 版 Available() 同名；返回是否可用。"""
        return self._driver is not None

    @property
    def driver(self):
        return self._driver

    # ─── Session ───
    def session(self):
        """返回 driver 自带 session（默认写模式）；调用方负责 close 或 with-block。"""
        if self._driver is None:
            return None
        return self._driver.session(default_access_mode="WRITE")

    # ─── 约束 / 索引 ───
    def ensure_constraints(self) -> None:
        """启动期幂等执行租户约束 / 索引迁移。

        只有复合实体约束创建成功后才删除旧的全局 name 约束，避免在目标
        Neo4j 版本不支持复合约束时主动放宽唯一性。此过程不迁移旧节点；
        旧节点没有 user_id，后续所有业务 Cypher 都会忽略它们。
        """
        if self._driver is None:
            return
        try:
            with self._driver.session(default_access_mode="WRITE") as sess:
                entity_constraint_ready = False
                try:
                    result = sess.run(_ENTITY_TENANT_CONSTRAINT)
                    consume = getattr(result, "consume", None)
                    if callable(consume):
                        consume()
                    entity_constraint_ready = True
                except Exception as e:
                    logger.warning("⚠️  Neo4j 租户复合约束创建失败，保留旧约束: %s", e)

                if entity_constraint_ready:
                    try:
                        result = sess.run(_DROP_LEGACY_ENTITY_CONSTRAINT)
                        consume = getattr(result, "consume", None)
                        if callable(consume):
                            consume()
                    except Exception as e:
                        logger.info("ℹ️  Neo4j constraint/index: %s", e)

                for q in _CONSTRAINTS[1:]:
                    try:
                        result = sess.run(q)
                        consume = getattr(result, "consume", None)
                        if callable(consume):
                            consume()
                    except Exception as e:
                        logger.info("ℹ️  Neo4j constraint/index: %s", e)
        except Exception as e:
            logger.warning("⚠️  Neo4j ensure_constraints 失败: %s", e)

    # ─── Cypher 执行 ───
    def run_cypher(self, query: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """执行 cypher 并返回结果（每行作为 dict）。失败返回空列表。"""
        if self._driver is None:
            return []
        try:
            with self._driver.session(default_access_mode="WRITE") as sess:
                result = sess.run(query, params or {})
                return [record.data() for record in result]
        except Exception as e:
            logger.warning("⚠️  Neo4j cypher 执行失败: %s", e)
            return []

    def execute_write(self, query: str, params: Optional[Dict[str, Any]] = None) -> bool:
        """便捷写操作；失败返回 False。"""
        if self._driver is None:
            return False
        try:
            with self._driver.session(default_access_mode="WRITE") as sess:
                sess.run(query, params or {})
            return True
        except Exception as e:
            logger.warning("⚠️  Neo4j 写操作失败: %s", e)
            return False

    # ─── 关闭 ───
    def close(self) -> None:
        if self._driver is not None:
            try:
                self._driver.close()
            except Exception as e:
                logger.warning("⚠️  Neo4j 关闭失败: %s", e)
            finally:
                self._driver = None
                self.status = "disconnected"
