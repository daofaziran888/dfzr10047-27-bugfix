"""重解析任务领域服务：按位点择定覆盖策略并编排历史批次重解析流程，产出重解析单数据集。"""
from __future__ import annotations

import threading
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 并发提交同一个实际变更的版本对时，建单、执行与完成都要串行化，只确认一次。
_TASK_LOCK = threading.RLock()


class ReparseTaskService(BaseModuleService):
    MODULE = "reparse_task"
    ENTITY = "重解析单"
    OPERATION_TERM = "回填"
    LIST_FIELDS = ["重解析编号", "关联批次", "重解析原因", "覆盖策略", "起始位点", "截止位点", "受影响投递数", "重解析结论"]
    REQUIRED_FIELDS = ["重解析编号", "关联批次", "重解析原因"]
    STATUS_ORDER = ["待执行", "执行中", "已完成", "被中止"]
    ACTION_RULES = {"启动重解析": "执行中", "确认完成": "已完成", "中止任务": "被中止"}
    NEGATIVE_ACTIONS = ["中止任务"]

    KEEP_OLD = "旧投递保留"
    REPLACE_OLD = "被新记录替代"
    STRATEGIES = (KEEP_OLD, REPLACE_OLD)

    def _find(self, task_no: str) -> dict[str, Any] | None:
        for row in store.rows(self.MODULE):
            if row.get("重解析编号") == task_no:
                return row
        return None

    def _diff_service(self):
        from app.services import SERVICES  # 延迟导入避免服务注册表循环引用
        return SERVICES["tmpl_diff"]

    @staticmethod
    def _range_receipt(task: dict[str, Any]) -> dict[str, Any] | None:
        """只有结论为「通过」的范围校验回执才算已验证范围，拒绝回执不算。"""
        receipt = task.get("范围校验回执")
        if not isinstance(receipt, dict):
            return None
        return receipt if receipt.get("结论") == "通过" else None

    @staticmethod
    def _raw_range_receipt(task: dict[str, Any]) -> dict[str, Any] | None:
        receipt = task.get("范围校验回执")
        return receipt if isinstance(receipt, dict) else None

    def _execution_receipt_recorded(self, task: dict[str, Any]) -> bool:
        """执行回执只允许签发一次：重复提交同一个实际变更不能得到第二张回执。"""
        return isinstance(task.get("执行回执"), dict) and bool(task["执行回执"])

    def _active_task_of_pair(self, diff_no: str) -> str:
        """查找同一实际变更版本对是否已有未终结的重解析单占用。"""
        diffs = self._diff_service()
        row = diffs.get_diff(diff_no)
        if row is None:
            return ""
        pair = diffs.pair_key(
            str(row.get("旧模板版本") or ""), str(row.get("新模板版本") or ""))
        pair_diffs = diffs.rows_of_pair(pair)
        reference: set[str] = set()
        for other in pair_diffs:
            task_no = str(other.get("重解析任务编号") or "")
            if task_no:
                reference.add(task_no)
        pair_diff_nos = {str(r.get("差异编号") or "") for r in pair_diffs} - {""}
        # 任务单也可能只在自己一侧登记了差异编号，双向都要查。
        reference.update(
            str(t.get("重解析编号") or "") for t in store.rows(self.MODULE)
            if str(t.get("差异编号") or "") in pair_diff_nos)
        for task_no in reference:
            task = self._find(task_no)
            if task is not None and str(task.get("status") or "") in ("待执行", "执行中"):
                return task_no
        return ""

    def plan_position_range(self, task_no: str, reason: str,
                            coverage_strategy: str, affected_batches: list[str],
                            batch_positions: dict[str, str],
                            diff_no: str = "") -> tuple[bool, str, dict[str, Any] | None]:
        with _TASK_LOCK:
            return self._plan_position_range(
                task_no, reason, coverage_strategy, affected_batches,
                batch_positions, diff_no)

    def _plan_position_range(self, task_no: str, reason: str,
                             coverage_strategy: str, affected_batches: list[str],
                             batch_positions: dict[str, str],
                             diff_no: str) -> tuple[bool, str, dict[str, Any] | None]:
        """按位点规划重解析范围：位点取受影响批次的最小/最大值。

        覆盖策略必须是已批准枚举；受影响批次为空时不建单。
        传入差异编号时必须先取得服务端的范围校验回执，零差异版本对不建单。
        """
        if coverage_strategy not in self.STRATEGIES:
            return False, (
                f"覆盖策略「{coverage_strategy}」不在允许范围 {list(self.STRATEGIES)}"
            ), None
        if not affected_batches:
            return False, "受影响批次为空，无需创建重解析单", None

        receipt: dict[str, Any] | None = None
        validated_batches = [b for b in affected_batches if b]
        if diff_no:
            diffs = self._diff_service()
            passed, receipt_msg, receipt = diffs.validate_reparse_range(
                diff_no, validated_batches, batch_positions,
                register_time=reason)
            if not passed or not receipt:
                return False, (
                    f"差异条目 {diff_no} 未取得范围校验回执，不得建单：{receipt_msg}"
                ), None
            validated_batches = [b for b in receipt.get("验证批次") or [] if b]
            if not validated_batches:
                return False, (
                    f"差异条目 {diff_no} 没有真正命中旧版本的批次，无需重解析"
                ), None

        positioned = {b: batch_positions[b] for b in validated_batches if b in batch_positions}
        if not positioned:
            return False, (
                f"受影响批次 {validated_batches[:5]} 均无位点信息，无法确定重解析范围"
            ), None

        ordered = sorted(positioned.items(), key=lambda kv: kv[1])
        start_position = ordered[0][1]
        end_position = ordered[-1][1]
        if start_position > end_position:
            return False, f"起始位点 {start_position} 晚于截止位点 {end_position}", None

        rows = store.rows(self.MODULE)
        existing = next((r for r in rows if r.get("重解析编号") == task_no), None)
        if existing is not None:
            if not self._execution_receipt_recorded(existing):
                return False, f"重解析单 {task_no} 已存在，不得重复创建", None

        if diff_no:
            clash = self._active_task_of_pair(diff_no)
            if clash:
                return False, (
                    f"版本对已由重解析单 {clash} 占用，同一实际变更只确认一次，"
                    f"不再重复建单"
                ), None

        entry_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        entry = {
            "id": entry_id, "status": "待执行", "pending": True, "abnormal": False,
            "重解析编号": task_no,
            "关联批次": ordered[0][0],
            "重解析原因": reason,
            "覆盖策略": coverage_strategy,
            "起始位点": start_position,
            "截止位点": end_position,
            "受影响投递数": str(len(ordered)),
            "受影响批次": ",".join(b for b, _ in ordered),
            "差异编号": diff_no,
            "范围校验回执": receipt,
            "重解析结论": "",
        }
        rows.append(entry)
        if diff_no and receipt:
            self._diff_service().record_reparse_task(diff_no, task_no)
        return True, (
            f"重解析单 {task_no} 已建单：范围 [{start_position}, {end_position}] "
            f"覆盖 {len(ordered)} 个批次，覆盖策略「{coverage_strategy}」"
        ), dict(entry)

    def record_task_execution(self, task_no: str,
                              handled_batches: list[str]) -> None:
        """把已验证范围实际处理的批次同步回模板差异台账。"""
        diff_no = str(task.get("差异编号") or "") if (task := self._find(task_no)) else ""
        if not diff_no:
            return
        self._diff_service().record_execution_result(
            diff_no, task_no, handled_batches,
            str(task.get("重解析结论") or ""))

    def batches_in_range(self, start_position: str, end_position: str) -> list[str]:
        """列出位点区间内的投递批次，供重解析执行范围核对。"""
        matched: list[str] = []
        for row in store.rows("file_intake"):
            batch_no = str(row.get("批次编号") or "")
            deliver_time = str(row.get("投递时间") or "")
            if not deliver_time:
                continue
            if start_position <= deliver_time <= end_position:
                matched.append(batch_no)
        return sorted(matched)

    def _template_sourced(self, row: dict[str, Any]) -> bool:
        """判断重解析单是否由模板差异驱动，模板差异驱动的单必须有通过回执。"""
        return bool(row.get("差异编号")) or row.get("来源类型") == "模板差异"

    def action_eligibility(self, row: dict[str, Any], action: str) -> tuple[bool, str]:
        """重解析单的可执行资格：没有通过回执或已中止的单不提供执行动作。"""
        if action not in self.ACTION_RULES:
            return False, f"动作「{action}」不属于{self.MODULE}可执行范围"
        status = str(row.get("status") or "")
        template_sourced = self._template_sourced(row)
        if action == "启动重解析":
            if status == "已完成":
                return False, f"重解析单 {row.get('重解析编号')} 已完成，不能重新启动"
            if status == "被中止":
                return False, f"重解析单 {row.get('重解析编号')} 已被中止，不能重新启动"
            if template_sourced and not self._range_receipt(row):
                return False, (
                    f"该重解析单来源模板差异未取得范围校验回执，不得执行"
                    f"（{row.get('重解析原因') or '模板差异'}）"
                )
            if self._execution_receipt_recorded(row):
                return False, "该重解析单已签发执行回执，不得重复执行"
            if status == "执行中":
                return False, "该重解析单正在执行中"
        elif action == "确认完成":
            if status in ("已完成", "被中止"):
                return False, f"重解析单 {row.get('重解析编号')} 当前状态为 {status}，不能确认完成"
            if template_sourced and not self._range_receipt(row):
                return False, "该重解析单来源模板差异未取得范围校验回执，不能确认完成"
        elif action == "中止任务" and status in ("已完成", "被中止"):
            return False, f"重解析单 {row.get('重解析编号')} 当前状态为 {status}，不能中止"
        return True, ""

    def detail(self, entry_id: int) -> dict[str, Any] | None:
        """重解析任务详情：带出已验证的重解析范围与拒绝原因。"""
        entry = self.get_entry(entry_id)
        if entry is None:
            return None
        ok, reason = self.action_eligibility(entry, "启动重解析")
        return {
            "entry": entry,
            "extra": {
                "已验证范围": {
                    "起始位点": entry.get("起始位点"),
                    "截止位点": entry.get("截止位点"),
                    "受影响批次": entry.get("受影响批次"),
                },
                "范围校验回执": entry.get("范围校验回执"),
                "执行回执": entry.get("执行回执"),
                "可执行": ok,
                "拒绝原因": "" if ok else reason,
            },
        }

    def start_task(self, task_no: str) -> tuple[bool, str]:
        """启动重解析：已完成/已中止的单不得重新执行。"""
        task = self._find(task_no)
        if task is None:
            return False, f"重解析单 {task_no} 不存在"
        ok, reason = self.action_eligibility(task, "启动重解析")
        if not ok:
            return False, reason
        task["status"] = "执行中"
        task["pending"] = True
        task["abnormal"] = False
        return True, f"重解析单 {task_no} 已开始执行"

    def complete_task(self, task_no: str, reparsed_count: int,
                      conclusion: str) -> tuple[bool, str, dict[str, Any] | None]:
        """确认完成：按覆盖策略处置旧投递记录，两种策略数据终态不同。

        完成时按已验证范围落到实际受影响批次，并把处理结果同步回模板差异台账；
        只有确有字段变更的版本对才签发执行回执，重复提交不会拿到第二张回执。
        """
        task = self._find(task_no)
        if task is None:
            return False, f"重解析单 {task_no} 不存在", None
        if task.get("status") == "已完成":
            return False, f"重解析单 {task_no} 已完成，不能重复确认", dict(task)
        if task.get("status") == "被中止":
            return False, f"重解析单 {task_no} 已被中止，不能确认完成", dict(task)

        with _TASK_LOCK:
            return self._complete_task_locked(task_no, reparsed_count, conclusion)

    def _complete_task_locked(self, task_no: str, reparsed_count: int,
                              conclusion: str) -> tuple[bool, str, dict[str, Any] | None]:
        task = self._find(task_no)
        if task is None:
            return False, f"重解析单 {task_no} 不存在", None
        if task.get("status") == "已完成":
            if self._execution_receipt_recorded(task):
                return False, (
                    f"重解析单 {task_no} 已签发执行回执，不能重复确认"
                ), dict(task)
            return False, f"重解析单 {task_no} 已完成，不能重复确认", dict(task)
        if task.get("status") == "被中止":
            return False, f"重解析单 {task_no} 已被中止，不能确认完成", dict(task)

        receipt = self._range_receipt(task)
        if task.get("差异编号") and not receipt:
            return False, f"重解析单 {task_no} 没有范围校验回执，不得确认完成", dict(task)

        strategy = str(task.get("覆盖策略") or "")
        if receipt is not None:
            affected = [
                b.strip() for b in (receipt.get("验证批次") or []) if str(b).strip()
            ]
        else:
            affected = [
                b.strip() for b in str(task.get("受影响批次") or "").split(",") if b.strip()
            ]
        handled = self._apply_coverage_strategy(strategy, affected, task_no)

        task["status"] = "已完成"
        task["pending"] = False
        task["abnormal"] = False
        task["受影响投递数"] = str(reparsed_count)
        task["重解析结论"] = f"{conclusion}；{handled}"
        if not self._execution_receipt_recorded(task):
            task["执行回执"] = {
                "回执编号": f"EX-{task_no}",
                "重解析编号": task_no,
                "差异编号": task.get("差异编号") or "",
                "处理批次": affected,
                "处理结论": task["重解析结论"],
                "已发布": True,
            }
        self.record_task_execution(task_no, affected)
        return True, (
            f"重解析单 {task_no} 已完成，重解析 {reparsed_count} 个批次，{handled}"
        ), dict(task)

    def _deliveries_of_batch(self, intake_batch: str) -> list[dict[str, Any]]:
        """沿真实引用链找出投递批次对应的全部投递记录。

        链路：投递批次 → 解析批次(关联投递批次) → 输出契约(关联批次=批次记录编号)
              → 投递记录(关联契约)。
        """
        batch_nos = {
            str(b.get("批次记录编号")) for b in store.rows("parse_batch")
            if str(b.get("关联投递批次")) == intake_batch
        }
        contract_nos = {
            str(c.get("契约编号")) for c in store.rows("output_contract")
            if str(c.get("关联批次")) in batch_nos
        }
        return [
            d for d in store.rows("result_deliver")
            if str(d.get("关联契约")) in contract_nos
        ]

    def affected_deliveries(self, intake_batch: str) -> list[str]:
        """输出重解析会影响到的投递记录编号，供受影响投递数核对。"""
        return [str(d.get("投递记录编号")) for d in self._deliveries_of_batch(intake_batch)]

    def _apply_coverage_strategy(self, strategy: str, affected: list[str],
                                 task_no: str) -> str:
        """执行覆盖策略对旧投递记录的处置。

        旧投递保留：旧记录状态不变，仅追加归档标记，下游可同时看到新旧两版。
        被新记录替代：旧记录标记为已作废，下游只看到新版投递。
        """
        touched = 0
        for batch_no in affected:
            for record in self._deliveries_of_batch(batch_no):
                touched += 1
                if strategy == self.KEEP_OLD:
                    record["归档标记"] = f"由 {task_no} 重解析产生新版本，旧投递保留可查"
                    record["新版本任务"] = task_no
                elif strategy == self.REPLACE_OLD:
                    record["status"] = "已作废"
                    record["pending"] = False
                    record["abnormal"] = True
                    record["投递结论"] = (
                        f"被 {task_no} 重解析结果替代，旧投递记录作废"
                    )
                    record["替代任务"] = task_no
        if strategy == self.KEEP_OLD:
            return f"旧投递保留：{touched} 条投递记录追加归档标记，新旧版本并存"
        if strategy == self.REPLACE_OLD:
            return f"被新记录替代：{touched} 条旧投递记录已作废，下游只读新版本"
        return "未识别的覆盖策略，未处置旧投递记录"

    def abort_task(self, task_no: str, reason: str) -> tuple[bool, str]:
        """中止任务：已完成的单不能中止，旧投递记录保持原状。"""
        task = self._find(task_no)
        if task is None:
            return False, f"重解析单 {task_no} 不存在"
        if task.get("status") == "已完成":
            return False, f"重解析单 {task_no} 已完成，不能中止"
        if task.get("status") == "被中止":
            return False, f"重解析单 {task_no} 已处于中止状态"
        task["status"] = "被中止"
        task["pending"] = False
        task["abnormal"] = True
        task["重解析结论"] = f"中止：{reason}"
        return True, f"重解析单 {task_no} 已中止：{reason}"

    def pending_tasks(self) -> list[dict[str, Any]]:
        """列出待执行/执行中的重解析单，供调度看板消费。"""
        return [
            {
                "重解析编号": r.get("重解析编号"),
                "关联批次": r.get("关联批次"),
                "覆盖策略": r.get("覆盖策略"),
                "起始位点": r.get("起始位点"),
                "截止位点": r.get("截止位点"),
                "status": r.get("status"),
            }
            for r in store.rows(self.MODULE)
            if r.get("status") in ("待执行", "执行中")
        ]
