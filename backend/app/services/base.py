"""模块服务基类：共享查询、登记、动作和统计骨架，业务差异留在各模块文件。"""
from __future__ import annotations

from typing import Any

from app.store import store


class BaseModuleService:
    MODULE = ""
    ENTITY = ""
    LIST_FIELDS: list[str] = []
    REQUIRED_FIELDS: list[str] = []
    STATUS_ORDER: list[str] = []
    ACTION_RULES: dict[str, str] = {}
    NEGATIVE_ACTIONS: list[str] = []

    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.rows(self.MODULE))
        for field, expected in (filters or {}).items():
            if field in self.LIST_FIELDS:
                rows = [
                    row for row in rows
                    if expected.casefold() in str(row.get(field, "")).casefold()
                ]
        if keyword and self.LIST_FIELDS:
            field = self.LIST_FIELDS[0]
            rows = [row for row in rows if keyword.casefold() in str(row.get(field, "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        page_rows = rows[start:start + size]
        return [self.decorate(row) for row in page_rows], total

    def decorate(self, row: dict[str, Any]) -> dict[str, Any]:
        """给每一行附带本行真正可执行的动作和资格说明，页面不再照搬全量动作。"""
        allowed: list[str] = []
        reasons: list[str] = []
        for action in self.ACTION_RULES:
            ok, reason = self.action_eligibility(row, action)
            if ok:
                allowed.append(action)
            elif reason:
                reasons.append(reason)
        decorated = dict(row)
        decorated["可执行动作"] = allowed
        decorated["资格说明"] = "；".join(dict.fromkeys(reasons))
        return decorated

    def action_eligibility(self, row: dict[str, Any], action: str) -> tuple[bool, str]:
        """默认动作资格：动作属于本模块即可执行，业务模块可覆盖收窄。"""
        if action not in self.ACTION_RULES:
            return False, f"动作「{action}」不属于{self.MODULE}可执行范围"
        return True, ""

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(self.MODULE, entry_id)
        return self.decorate(entry) if entry is not None else None

    def detail(self, entry_id: int) -> dict[str, Any] | None:
        """比较详情/任务详情：返回条目本身与可核对的附加结论。"""
        entry = self.get_entry(entry_id)
        if entry is None:
            return None
        return {"entry": entry, "extra": {}}

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [
            field for field in self.REQUIRED_FIELDS
            if not str(values.get(field) or "").strip()
        ]
        if missing:
            return None, missing
        rows = store.rows(self.MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in self.LIST_FIELDS})
        entry["status"] = self.STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return self.decorate(entry), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        target = self.ACTION_RULES.get(action)
        if target is None:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"
        ok, reason = self.action_eligibility(entry, action)
        if not ok:
            return None, reason
        entry["status"] = target
        entry["pending"] = target != self.STATUS_ORDER[-1]
        entry["abnormal"] = action in self.NEGATIVE_ACTIONS
        return self.decorate(entry), f"{self.ENTITY}已{action}"

    def domain_snapshot(self) -> dict[str, Any]:
        """输出模块级数据质量快照，供跨模块消费和题位源码锚点使用。"""
        rows = store.rows(self.MODULE)
        by_status = {
            status: sum(1 for row in rows if row.get("status") == status)
            for status in self.STATUS_ORDER
        }
        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}数据集",
            "count": len(rows),
            "by_status": by_status,
            "abnormal": sum(1 for row in rows if row.get("abnormal")),
        }
