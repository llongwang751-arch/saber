"""Transactional persistence facade for auth, skills, and smart-farm features."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import date
from typing import Any, Iterator

from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from internal.evaluation.store import EvaluationStore

from .models import FarmProductionRecord, FarmReportRecord, InstalledSkillRecord, UserRecord, utcnow


class ConflictError(RuntimeError):
    pass


class NotFoundError(RuntimeError):
    pass


class ApplicationStore:
    """Shares the evaluation database and its Alembic lifecycle."""

    def __init__(self, database_url: str | None = None):
        self._evaluation_store = EvaluationStore(database_url=database_url)
        self.engine = self._evaluation_store.engine
        self._sessions = sessionmaker(bind=self.engine, expire_on_commit=False, class_=Session)

    def close(self) -> None:
        self._evaluation_store.close()

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        session = self._sessions()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- users ---------------------------------------------------------------
    def create_user(
        self,
        username: str,
        password_hash: str,
        *,
        tenant_id: str | None = None,
        roles: list[str] | None = None,
        identity_provenance: str = "self_service",
        experiment_eligible: bool = False,
    ) -> dict[str, Any]:
        user_id = str(uuid.uuid4())
        normalized_roles = _roles(roles)
        provenance, eligible = _experiment_identity(
            identity_provenance,
            experiment_eligible,
            normalized_roles,
        )
        record = UserRecord(
            id=user_id,
            username=username,
            password_hash=password_hash,
            tenant_id=_tenant_id(tenant_id or user_id),
            roles=normalized_roles,
            identity_provenance=provenance,
            experiment_eligible=eligible,
        )
        try:
            with self.transaction() as session:
                session.add(record)
                session.flush()
        except IntegrityError as exc:
            raise ConflictError("用户名已存在") from exc
        return self._user(record)

    def find_user_by_username(self, username: str, *, include_secret: bool = False) -> dict[str, Any]:
        with self.transaction() as session:
            record = session.scalar(select(UserRecord).where(UserRecord.username == username))
            if record is None:
                raise NotFoundError("用户不存在")
            result = self._user(record)
            if include_secret:
                result["password_hash"] = record.password_hash
            return result

    def find_user(self, user_id: str) -> dict[str, Any]:
        with self.transaction() as session:
            record = session.get(UserRecord, user_id)
            if record is None:
                raise NotFoundError("用户不存在")
            return self._user(record)

    def list_user_ids_for_tenant(self, tenant_id: str) -> list[str]:
        """Internal-only lookup used to locate tenant-owned offline evidence."""

        with self.transaction() as session:
            return list(
                session.scalars(
                    select(UserRecord.id).where(UserRecord.tenant_id == _tenant_id(tenant_id))
                ).all()
            )

    def touch_last_login(self, user_id: str) -> None:
        with self.transaction() as session:
            record = session.get(UserRecord, user_id)
            if record is not None:
                record.last_login_at = utcnow()

    def set_user_identity(
        self,
        user_id: str,
        *,
        tenant_id: str | None = None,
        roles: list[str] | None = None,
        identity_provenance: str | None = None,
        experiment_eligible: bool | None = None,
    ) -> dict[str, Any]:
        """Persist identity attributes sourced only from server configuration."""

        with self.transaction() as session:
            record = session.get(UserRecord, user_id)
            if record is None:
                raise NotFoundError("用户不存在")
            next_tenant = (
                _tenant_id(tenant_id) if tenant_id is not None else record.tenant_id
            )
            next_roles = _roles(roles) if roles is not None else _roles(record.roles)
            next_provenance = (
                identity_provenance
                if identity_provenance is not None
                else record.identity_provenance
            )
            next_eligible = (
                bool(experiment_eligible)
                if experiment_eligible is not None
                else bool(record.experiment_eligible)
            )
            # Moving an account to another tenant is a new trust decision.  A
            # caller must explicitly re-assert eligibility in the same update.
            if next_tenant != record.tenant_id and experiment_eligible is None:
                next_eligible = False
            # A later server-side role sync must never leave an administrator
            # or approver inside the experiment population.
            if _has_privileged_experiment_role(next_roles):
                if experiment_eligible is True:
                    raise ValueError("实验管理员和审批人不能标记为真实实验用户")
                next_eligible = False
            provenance, eligible = _experiment_identity(
                next_provenance,
                next_eligible,
                next_roles,
            )
            record.tenant_id = next_tenant
            record.roles = next_roles
            record.identity_provenance = provenance
            record.experiment_eligible = eligible
            session.flush()
            return self._user(record)

    @staticmethod
    def _user(record: UserRecord) -> dict[str, Any]:
        return {
            "id": record.id,
            "user_id": record.id,
            "username": record.username,
            "tenant_id": record.tenant_id,
            "roles": _roles(record.roles),
            "identity_provenance": record.identity_provenance,
            "experiment_eligible": bool(record.experiment_eligible),
            "created_at": record.created_at,
            "last_login_at": record.last_login_at,
        }

    # -- skills --------------------------------------------------------------
    def install_skill(self, user_id: str, manifest: dict[str, Any]) -> dict[str, Any]:
        with self.transaction() as session:
            record = session.scalar(
                select(InstalledSkillRecord).where(
                    InstalledSkillRecord.user_id == user_id,
                    InstalledSkillRecord.skill_id == manifest["id"],
                )
            )
            if record is None:
                record = InstalledSkillRecord(id=str(uuid.uuid4()), user_id=user_id, skill_id=manifest["id"])
                session.add(record)
            for field in (
                "name", "description", "category", "source", "source_url", "stars",
                "invocation", "endpoint", "prompt_template", "parameters",
            ):
                setattr(record, field, manifest.get(field) or ([] if field == "parameters" else 0 if field == "stars" else ""))
            if record.enabled is None:
                record.enabled = True
            record.updated_at = utcnow()
            session.flush()
            return self._skill(record)

    def list_skills(self, user_id: str, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        query = select(InstalledSkillRecord).where(InstalledSkillRecord.user_id == user_id)
        if enabled_only:
            query = query.where(InstalledSkillRecord.enabled.is_(True))
        query = query.order_by(InstalledSkillRecord.installed_at.desc())
        with self.transaction() as session:
            return [self._skill(item) for item in session.scalars(query).all()]

    def set_skill_enabled(self, user_id: str, skill_id: str, enabled: bool) -> dict[str, Any]:
        with self.transaction() as session:
            record = session.scalar(select(InstalledSkillRecord).where(
                InstalledSkillRecord.user_id == user_id,
                InstalledSkillRecord.skill_id == skill_id,
            ))
            if record is None:
                raise NotFoundError("skill 未安装")
            record.enabled = enabled
            record.updated_at = utcnow()
            session.flush()
            return self._skill(record)

    def uninstall_skill(self, user_id: str, skill_id: str) -> None:
        with self.transaction() as session:
            record = session.scalar(select(InstalledSkillRecord).where(
                InstalledSkillRecord.user_id == user_id,
                InstalledSkillRecord.skill_id == skill_id,
            ))
            if record is None:
                raise NotFoundError("skill 未安装")
            session.delete(record)

    @staticmethod
    def _skill(record: InstalledSkillRecord) -> dict[str, Any]:
        return {
            "id": record.skill_id,
            "skill_id": record.skill_id,
            "name": record.name,
            "description": record.description,
            "category": record.category,
            "source": record.source,
            "source_url": record.source_url,
            "stars": record.stars,
            "invocation": record.invocation,
            "endpoint": record.endpoint,
            "prompt_template": record.prompt_template,
            "parameters": record.parameters or [],
            "user_id": record.user_id,
            "enabled": record.enabled,
            "installed_at": record.installed_at,
        }

    # -- farm ----------------------------------------------------------------
    def insert_farm_records(self, user_id: str, records: list[dict[str, Any]]) -> tuple[int, int]:
        accepted = 0
        duplicate = 0
        with self.transaction() as session:
            for item in records:
                exists = session.scalar(select(FarmProductionRecord.id).where(
                    FarmProductionRecord.user_id == user_id,
                    FarmProductionRecord.source_hash == item["source_hash"],
                    FarmProductionRecord.source_row == item["source_row"],
                ))
                if exists:
                    duplicate += 1
                    continue
                session.add(FarmProductionRecord(user_id=user_id, **item))
                accepted += 1
        return accepted, duplicate

    def list_farm_records(
        self, user_id: str, *, date_from: date | None = None, date_to: date | None = None,
        farm_name: str = "", stage: str = "", limit: int = 500,
    ) -> list[dict[str, Any]]:
        filters = [FarmProductionRecord.user_id == user_id]
        if date_from:
            filters.append(FarmProductionRecord.record_date >= date_from)
        if date_to:
            filters.append(FarmProductionRecord.record_date <= date_to)
        if farm_name:
            filters.append(FarmProductionRecord.farm_name == farm_name)
        if stage:
            filters.append(FarmProductionRecord.stage == stage)
        query = select(FarmProductionRecord).where(and_(*filters)).order_by(
            FarmProductionRecord.record_date.desc(), FarmProductionRecord.id
        ).limit(max(1, min(int(limit), 100001)))
        with self.transaction() as session:
            return [self._farm_record(record) for record in session.scalars(query).all()]

    def save_farm_report(self, report: dict[str, Any]) -> dict[str, Any]:
        record = FarmReportRecord(**report)
        with self.transaction() as session:
            session.add(record)
            session.flush()
        return self._farm_report(record)

    def list_farm_reports(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        query = select(FarmReportRecord).where(FarmReportRecord.user_id == user_id).order_by(
            FarmReportRecord.created_at.desc()
        ).limit(max(1, min(int(limit), 200)))
        with self.transaction() as session:
            return [self._farm_report(record) for record in session.scalars(query).all()]

    def get_farm_report(self, user_id: str, report_id: str) -> dict[str, Any]:
        with self.transaction() as session:
            record = session.scalar(select(FarmReportRecord).where(
                FarmReportRecord.user_id == user_id,
                FarmReportRecord.id == report_id,
            ))
            if record is None:
                raise NotFoundError("报告不存在")
            return self._farm_report(record)

    @staticmethod
    def _farm_record(record: FarmProductionRecord) -> dict[str, Any]:
        return {column.name: getattr(record, column.name) for column in record.__table__.columns if column.name != "user_id"}

    @staticmethod
    def _farm_report(record: FarmReportRecord) -> dict[str, Any]:
        return {
            "id": record.id,
            "type": record.report_type,
            "farm_name": record.farm_name,
            "from": record.date_from,
            "to": record.date_to,
            "metrics": record.metrics or [],
            "anomalies": record.anomalies or [],
            "data_quality": record.data_quality or {},
            "markdown": record.markdown,
            "document_id": record.document_id,
            "created_at": record.created_at,
        }


_KNOWN_ROLES = frozenset({
    "participant",
    "experiment_admin",
    "experiment_approver",
})
_PRIVILEGED_EXPERIMENT_ROLES = frozenset({
    "experiment_admin",
    "experiment_approver",
})
_IDENTITY_PROVENANCE_VALUES = frozenset({
    "self_service",
    "development_seed",
    "legacy_unverified",
    "operator_provisioned",
    "trusted_sso",
})
_TRUSTED_EXPERIMENT_PROVENANCE = frozenset({
    "operator_provisioned",
    "trusted_sso",
})


def _tenant_id(value: str) -> str:
    result = str(value or "").strip()
    if not result or "\x00" in result or len(result) > 64:
        raise ValueError("tenant_id 必须是 1 到 64 个有效字符")
    return result


def _roles(value: Any) -> list[str]:
    raw = value if isinstance(value, (list, tuple, set)) else ["participant"]
    result = {str(item).strip() for item in raw if str(item).strip()}
    unknown = result - _KNOWN_ROLES
    if unknown:
        raise ValueError(f"未知用户角色: {sorted(unknown)}")
    result.add("participant")
    return sorted(result)


def _has_privileged_experiment_role(roles: Any) -> bool:
    return bool(set(_roles(roles)) & _PRIVILEGED_EXPERIMENT_ROLES)


def _experiment_identity(
    provenance: str,
    eligible: bool,
    roles: Any,
) -> tuple[str, bool]:
    normalized = str(provenance or "").strip()
    if normalized not in _IDENTITY_PROVENANCE_VALUES:
        raise ValueError(
            "identity_provenance 必须是受支持的服务端身份来源"
        )
    if not isinstance(eligible, bool):
        raise ValueError("experiment_eligible 必须是布尔值")
    if eligible and normalized not in _TRUSTED_EXPERIMENT_PROVENANCE:
        raise ValueError("只有后台预置或可信单点登录账号可进入真实实验")
    if eligible and _has_privileged_experiment_role(roles):
        raise ValueError("实验管理员和审批人不能标记为真实实验用户")
    return normalized, eligible
