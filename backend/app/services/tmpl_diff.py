"""模板差异领域服务：逐字段比对模板新旧版本抽取表达式变更，产出差异条目数据集。"""
from __future__ import annotations

import threading
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 同一版本对的比较与回执签发可能被并发提交，确认动作必须串行化后只落一次。
_RECEIPT_LOCK = threading.Lock()


class TmplDiffService(BaseModuleService):
    MODULE = "tmpl_diff"
    ENTITY = "差异条目"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["差异编号", "旧模板版本", "新模板版本", "差异字段", "差异类别", "影响批次数", "修正方案", "登记时间"]
    REQUIRED_FIELDS = ["差异编号", "旧模板版本", "新模板版本"]
    STATUS_ORDER = ["待比对", "比对中", "已收敛", "偏离超限"]
    ACTION_RULES = {"启动比对": "比对中", "确认收敛": "已收敛", "标记超限": "偏离超限"}
    NEGATIVE_ACTIONS = ["标记超限"]

    # 差异字段数超过该上限时判定偏离超限，必须分批重解析而非一次性覆盖
    MAX_SINGLE_PASS_FIELDS = 6

    ZERO_DIFF_REASON = (
        "新旧版本抽取表达式逐字段一致，没有实际字段差异，不授权处理历史批次"
    )

    def _find_tmpl(self, version_no: str) -> dict[str, Any] | None:
        for row in store.rows("extract_tmpl"):
            if row.get("版本号") == version_no:
                return row
        return None

    def get_diff(self, diff_no: str) -> dict[str, Any] | None:
        """按差异编号取差异条目，供重解析和页面比较详情共用。"""
        for row in store.rows(self.MODULE):
            if row.get("差异编号") == diff_no:
                return row
        return None

    @staticmethod
    def pair_key(old_version: str, new_version: str) -> str:
        return f"{str(old_version).strip()}->{str(new_version).strip()}"

    def _find_pair_diff(self, old_version: str, new_version: str,
                        exclude: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """按版本对查找已确认的差异条目，用于同一实际变更只确认一次。"""
        key = self.pair_key(old_version, new_version)
        for row in store.rows(self.MODULE):
            if row is exclude:
                continue
            if self.pair_key(row.get("旧模板版本", ""), row.get("新模板版本", "")) != key:
                continue
            receipt = row.get("范围校验回执")
            if isinstance(receipt, dict) and receipt.get("结论") == "通过":
                return row
        return None

    def make_receipt(self, diff_no: str, old_version: str, new_version: str,
                     fields: list[str], verified_batches: list[str],
                     start_position: str, end_position: str, register_time: str,
                     *, zero_diff: bool, reason: str = "",
                     reused_from: str = "") -> dict[str, Any]:
        """构造范围校验回执：通过或拒绝都留痕，页面与重解析任务都按回执判断。"""
        return {
            "回执编号": f"RC-{diff_no}",
            "差异编号": diff_no,
            "版本对": self.pair_key(old_version, new_version),
            "结论": "拒绝" if zero_diff else "通过",
            "原因": reason if zero_diff else "",
            "命中字段": ",".join(fields),
            "验证批次": list(verified_batches),
            "起始位点": "" if zero_diff else start_position,
            "截止位点": "" if zero_diff else end_position,
            "确认时间": register_time,
            "复用自": reused_from,
        }

    def compare_versions(self, diff_no: str, old_version: str, new_version: str,
                         old_fields: list[str], new_fields: list[str],
                         changed_fields: list[str], affected_batches: int,
                         register_time: str,
                         affected_batch_list: list[str] | None = None) -> tuple[bool, str, dict[str, Any] | None]:
        """逐字段比对新旧版本，按差异字段数判定收敛或偏离超限。

        差异条目记录新增/删除/变更字段清单，影响批次数决定重解析位点范围。
        零差异版本对只保留可核对的比较结论，不授权处理任何历史批次。
        """
        old_tmpl = self._find_tmpl(old_version)
        new_tmpl = self._find_tmpl(new_version)
        if old_tmpl is None or new_tmpl is None:
            missing = old_version if old_tmpl is None else new_version
            return False, f"模板版本 {missing} 在抽取模板中不存在，无法比对", None

        added = sorted(set(new_fields) - set(old_fields))
        removed = sorted(set(old_fields) - set(new_fields))
        changed = sorted(set(changed_fields) & (set(old_fields) & set(new_fields)))
        diff_count = len(added) + len(removed) + len(changed)

        categories: list[str] = []
        if added:
            categories.append(f"新增{len(added)}")
        if removed:
            categories.append(f"删除{len(removed)}")
        if changed:
            categories.append(f"变更{len(changed)}")

        zero_diff = diff_count == 0
        converged = diff_count <= self.MAX_SINGLE_PASS_FIELDS
        status = "已收敛" if converged else "偏离超限"
        conclusion = (
            f"模板 {old_version}→{new_version} 逐字段比对一致，零变化，仅保留比较结论"
            if zero_diff else
            f"模板 {old_version}→{new_version} 差异 {diff_count} 个字段"
        )
        if zero_diff:
            plan = "新旧版本抽取表达式一致，无需重解析"
            executable = "否"
            reject_reason = self.ZERO_DIFF_REASON
        elif converged:
            plan = f"差异 {diff_count} 个字段在单趟上限内，按位点范围一次性重解析 {affected_batches} 个批次"
            executable = "是"
            reject_reason = ""
        else:
            chunks = -(-diff_count // self.MAX_SINGLE_PASS_FIELDS)
            plan = (
                f"差异 {diff_count} 个字段超过单趟上限 {self.MAX_SINGLE_PASS_FIELDS}，"
                f"影响 {affected_batches} 个批次须分 {chunks} 批重解析，禁止一次性覆盖"
            )
            executable = "是"
            reject_reason = ""

        verified: list[str] = []
        if not zero_diff:
            verified = list(affected_batch_list or [])
        # 比较本身只给零差异一条拒绝回执；有变更的通过回执必须由范围校验按
        # 真正命中的批次和位点签发，比较阶段不预先授权执行。
        existing = next(
            (r for r in store.rows(self.MODULE) if r.get("差异编号") == diff_no), None)
        prior_receipt = existing.get("范围校验回执") if existing else None
        receipt: dict[str, Any] | None
        if zero_diff:
            receipt = self.make_receipt(
                diff_no, old_version, new_version,
                added + removed + changed, [], "", "", register_time,
                zero_diff=True, reason=reject_reason)
        elif isinstance(prior_receipt, dict):
            receipt = prior_receipt
        else:
            receipt = None

        with _RECEIPT_LOCK:
            rows = store.rows(self.MODULE)
            target = next((r for r in rows if r.get("差异编号") == diff_no), None)
            if target is None:
                entry_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
                target = {"id": entry_id, "差异编号": diff_no}
                rows.append(target)
            if receipt is not None:
                target["范围校验回执"] = receipt
            target.update({
                "旧模板版本": old_version,
                "新模板版本": new_version,
                "差异字段": ",".join(added + removed + changed),
                "差异类别": ",".join(categories) or "无差异",
                "影响批次数": "0" if zero_diff else str(affected_batches),
                "观察批次数": str(affected_batches),
                "观察批次": ",".join(affected_batch_list or []),
                "修正方案": plan,
                "比较结论": conclusion,
                "登记时间": register_time,
                "新增字段": ",".join(added),
                "删除字段": ",".join(removed),
                "变更字段": ",".join(changed),
                "差异字段数": str(diff_count),
                "零差异": "是" if zero_diff else "否",
                "可执行重解析": executable,
                "拒绝原因": reject_reason,
                "命中批次": ",".join(verified),
                "命中版本": self.pair_key(old_version, new_version),
                "status": status,
                "pending": not converged,
                "abnormal": not converged,
            })
        return converged, (
            f"模板 {old_version}→{new_version} 差异 {diff_count} 个字段，"
            f"判定{status}：{plan}"
        ), dict(target)

    def validate_reparse_range(self, diff_no: str,
                               affected_batches: list[str],
                               batch_positions: dict[str, str],
                               hit_versions: dict[str, str] | None = None,
                               register_time: str = "") -> tuple[bool, str, dict[str, Any] | None]:
        """服务端范围校验：只有确有字段变更且真正命中旧版本的批次才签发通过回执。

        零差异版本对一律拒绝；同一版本对重复提交复用首个回执，不重复确认。
        """
        with _RECEIPT_LOCK:
            row = self.get_diff(diff_no)
            if row is None:
                return False, f"差异条目 {diff_no} 不存在，无法校验重解析范围", None

            fields = self._split_fields(row.get("差异字段"))
            diff_count = self._diff_count(row)
            if diff_count == 0 or not fields:
                receipt = row.get("范围校验回执")
                if not isinstance(receipt, dict) or receipt.get("结论") != "拒绝":
                    receipt = self.make_receipt(
                        diff_no, str(row.get("旧模板版本") or ""),
                        str(row.get("新模板版本") or ""), fields, [], "", "",
                        register_time, zero_diff=True, reason=self.ZERO_DIFF_REASON)
                    row["范围校验回执"] = receipt
                row["可执行重解析"] = "否"
                row["拒绝原因"] = self.ZERO_DIFF_REASON
                row["影响批次数"] = "0"
                return False, (
                    f"差异条目 {diff_no} 零差异，{self.ZERO_DIFF_REASON}，"
                    f"不签发执行回执"
                ), receipt

            # 同一实际变更的版本对只确认一次：后到的提交复用首个通过回执。
            paired = self._find_pair_diff(
                str(row.get("旧模板版本") or ""), str(row.get("新模板版本") or ""),
                exclude=row)
            if paired is not None:
                receipt = paired["范围校验回执"]
                receipt = dict(receipt)
                receipt["复用自"] = str(paired.get("差异编号") or "")
                row["范围校验回执"] = receipt
                row["可执行重解析"] = "是"
                row["拒绝原因"] = ""
                row["重解析任务编号"] = str(paired.get("重解析任务编号") or "")
                return True, (
                    f"版本对 {receipt.get('版本对')} 已确认，复用回执 "
                    f"{receipt.get('回执编号')}，不再重复签发"
                ), receipt

            verified = [b for b in affected_batches if b]
            if hit_versions is not None:
                acceptable = {
                    str(row.get("旧模板版本") or ""),
                    str(row.get("旧模板编号") or ""),
                } - {""}
                verified = [
                    b for b in verified
                    if str(hit_versions.get(b, "")) in acceptable
                ]
            positioned = {b: batch_positions[b] for b in verified if b in batch_positions}
            if not positioned:
                reason = (
                    "没有任何批次真正命中旧版本，实际变更不影响历史批次"
                    if hit_versions is not None and verified == [] else
                    "受影响批次均无位点信息，无法确定重解析范围"
                )
                receipt = self.make_receipt(
                    diff_no, str(row.get("旧模板版本") or ""),
                    str(row.get("新模板版本") or ""), fields, verified, "", "",
                    register_time, zero_diff=True, reason=reason)
                row["范围校验回执"] = receipt
                row["可执行重解析"] = "否"
                row["拒绝原因"] = reason
                row["影响批次数"] = "0"
                return False, f"差异条目 {diff_no} 不签发执行回执：{reason}", receipt

            prior = row.get("范围校验回执")
            if isinstance(prior, dict) and prior.get("结论") == "通过":
                return True, (
                    f"差异条目 {diff_no} 已有范围校验回执 {prior.get('回执编号')}，"
                    f"不再重复确认"
                ), prior

            ordered = sorted(positioned.items(), key=lambda kv: kv[1])
            start_position = ordered[0][1]
            end_position = ordered[-1][1]
            if start_position > end_position:
                return False, (
                    f"起始位点 {start_position} 晚于截止位点 {end_position}"
                ), None

            receipt = self.make_receipt(
                diff_no, str(row.get("旧模板版本") or ""),
                str(row.get("新模板版本") or ""), fields,
                [b for b, _ in ordered], start_position, end_position,
                register_time, zero_diff=False)
            row["范围校验回执"] = receipt
            row["可执行重解析"] = "是"
            row["拒绝原因"] = ""
            row["影响批次数"] = str(len(ordered))
            row["命中批次"] = ",".join(b for b, _ in ordered)
            return True, (
                f"差异条目 {diff_no} 范围校验通过，回执 {receipt['回执编号']}："
                f"越 [{start_position}, {end_position}] 覆盖 {len(ordered)} 个批次"
            ), receipt

    def resolve_eligibility(self, diff_no: str,
                            batch: str = "") -> dict[str, Any] | None:
        """输出比较详情页需要的资格结论：是否能执行、拒绝原因与回执。"""
        row = self.get_diff(diff_no)
        if row is None:
            return None
        fields = self.diff_fields_of(diff_no)
        zero_diff = self._is_zero_diff(row)
        task_no = str(row.get("重解析任务编号") or "")
        task = next(
            (r for r in store.rows("reparse_task")
             if str(r.get("重解析编号") or "") == task_no), None) if task_no else None
        if task is None:
            # 双向回查：重解析单也可能只在自己一侧登记了差异编号。
            task = next(
                (r for r in store.rows("reparse_task")
                 if str(r.get("差异编号") or "") == diff_no), None)
            if task is not None:
                task_no = str(task.get("重解析编号") or "")
        task_status = str(task.get("status") or "") if task else ""

        reason = str(row.get("拒绝原因") or "")
        task_executed = task_status == "已完成"
        pending_compare = self._comparison_pending(row)
        if pending_compare:
            executable = False
            zero_diff = False
            reason = "逐字段比对尚未完成，暂不能执行重解析"
        elif zero_diff:
            executable = False
            reason = reason or self.ZERO_DIFF_REASON
        elif row.get("可执行重解析") == "否":
            executable = False
            reason = reason or "该条目没有取得通过的范围校验回执，不能执行重解析"
        elif task_executed:
            # 已执行完成的版本对不再有可执行动作，但回执和产物仍可打开核对。
            executable = False
            reason = f"该版本对已由重解析单 {task_no} 处理完成，不再重复执行"
        elif task_status == "执行中":
            executable = False
            reason = f"该版本对正由重解析单 {task_no} 执行中，尚未发布"
        elif task_status == "待执行":
            executable = False
            reason = f"该版本对已由重解析单 {task_no} 占用待执行，不再重复建单"
        else:
            executable = True

        return {
            "差异编号": diff_no,
            "版本对": self.pair_key(
                str(row.get("旧模板版本") or ""), str(row.get("新模板版本") or "")),
            "零变化结论": (
                f"模板 {row.get('旧模板版本')}→{row.get('新模板版本')} "
                f"逐字段比对一致，零变化" if zero_diff else ""
            ),
            "比较结论": row.get("比较结论") or row.get("修正方案") or (
                "" if pending_compare else ""
            ),
            "比对状态": "未完成" if pending_compare else "已完成",
            "差异字段": fields,
            "可执行": executable,
            "拒绝原因": "" if executable else reason,
            "已确认产物可打开": bool(row.get("执行回执")),
            "可继续执行": executable,
            "范围校验回执": row.get("范围校验回执"),
            "重解析任务编号": task_no,
            "任务状态": task_status,
            "比较详情可打开": True,
            "产物可核对": True,
            "查询批次": batch,
        }

    def applications_for_batch(self, batch: str) -> list[dict[str, Any]]:
        """按批次回查全部差异申请，标出哪份仍可执行、哪份已被另一版本占用。

        返回结果按登记时间排序，供页面在确有变更但投入另一版处理时选择可执行申请。
        """
        rows = store.rows(self.MODULE)
        ordered = sorted(rows, key=lambda r: (str(r.get("登记时间") or ""),
                                              str(r.get("差异编号") or "")))
        applications: list[dict[str, Any]] = []
        for row in ordered:
            diff_no = str(row.get("差异编号") or "")
            related = {
                b.strip()
                for field in ("命中批次", "观察批次", "已执行批次")
                for b in str(row.get(field) or "").split(",") if b.strip()
            }
            observed = self._int_value(row.get("观察批次数"))
            if batch and batch not in related and not observed:
                continue
            eligibility = self.resolve_eligibility(diff_no, batch=batch)
            if eligibility is None:
                continue
            applications.append({
                "差异编号": diff_no,
                "旧模板版本": row.get("旧模板版本"),
                "新模板版本": row.get("新模板版本"),
                "登记时间": row.get("登记时间"),
                "可执行": eligibility["可执行"],
                "拒绝原因": eligibility["拒绝原因"],
                "零变化结论": eligibility["零变化结论"],
                "范围校验回执": eligibility["范围校验回执"],
                "重解析任务编号": eligibility["重解析任务编号"],
                "任务状态": eligibility["任务状态"],
            })
        return applications

    def rows_of_pair(self, pair: str) -> list[dict[str, Any]]:
        """列出同一版本对下的全部差异条目，供重解析判断是否已有申请占用。"""
        return [
            r for r in store.rows(self.MODULE)
            if self.pair_key(r.get("旧模板版本", ""), r.get("新模板版本", "")) == pair
        ]

    def record_reparse_task(self, diff_no: str, task_no: str) -> None:
        """登记差异条目对应的重解析单，页面据此判断是否已有申请占用。"""
        row = self.get_diff(diff_no)
        if row is None:
            return
        row["重解析任务编号"] = task_no

    def record_execution_result(self, diff_no: str, task_no: str,
                                handled_batches: list[str],
                                conclusion: str) -> None:
        """重解析任务完成后把实际受影响批次的处理结果同步回模板差异台账。"""
        row = self.get_diff(diff_no)
        if row is None:
            return
        with _RECEIPT_LOCK:
            row["重解析任务编号"] = task_no
            row["已执行批次"] = ",".join(handled_batches)
            row["处理结果"] = conclusion
            row["执行回执"] = {
                "回执编号": f"EX-{task_no}",
                "差异编号": diff_no,
                "重解析编号": task_no,
                "处理批次": list(handled_batches),
                "处理结论": conclusion,
                "已发布": True,
            }

    def action_eligibility(self, row: dict[str, Any], action: str) -> tuple[bool, str]:
        """零差异条目只保留比较结论，不得借此确认收敛或授权历史批次处理。"""
        if action not in self.ACTION_RULES:
            return False, f"动作「{action}」不属于{self.MODULE}可执行范围"
        if self._comparison_pending(row):
            if action in ("确认收敛", "标记超限"):
                return False, "逐字段比对尚未完成，不能直接下收敛或超限结论"
            return True, ""
        if self._is_zero_diff(row):
            if action == "确认收敛":
                return False, f"零差异条目无需确认收敛：{self.ZERO_DIFF_REASON}"
            if action == "启动比对":
                return False, "已确认零变化的条目只保留比较结论，不再重复比对"
        if action == "确认收敛" and row.get("可执行重解析") == "否":
            return False, str(row.get("拒绝原因") or "该条目未取得执行回执，不能确认收敛")
        return True, ""

    def detail(self, entry_id: int) -> dict[str, Any] | None:
        """比较详情：保留可打开的零变化结论、拒绝原因与范围校验回执。"""
        entry = self.get_entry(entry_id)
        if entry is None:
            return None
        diff_no = str(entry.get("差异编号") or "")
        eligibility = self.resolve_eligibility(diff_no) if diff_no else None
        return {
            "entry": entry,
            "extra": {
                "资格结论": eligibility,
                "范围校验回执": entry.get("范围校验回执"),
                "处理结果": entry.get("处理结果") or "",
                "执行回执": entry.get("执行回执"),
                "比较详情可打开": True,
                "产物可核对": True,
            },
        }

    def affected_batches_of(self, diff_no: str) -> int:
        """输出差异条目影响批次数，供重解析位点范围规划消费。"""
        row = self.get_diff(diff_no)
        if row is None:
            return 0
        return self._int_value(row.get("影响批次数"))

    @staticmethod
    def _int_value(value: Any) -> int:
        text = str(value or "").strip()
        return int(text) if text.isdigit() else 0

    def _diff_count(self, row: dict[str, Any]) -> int:
        """差异字段数：优先取登记值，缺省时按差异字段清单回算，避免旧条目被误当零差异。"""
        raw = str(row.get("差异字段数") or "").strip()
        if raw.isdigit():
            return int(raw)
        return len(self._split_fields(row.get("差异字段")))

    @staticmethod
    def _split_fields(value: Any) -> list[str]:
        return [f.strip() for f in str(value or "").split(",") if f.strip()]

    def _is_zero_diff(self, row: dict[str, Any]) -> bool:
        """只有确已完成逐字段比对且字段清单为空，才算零差异。

        尚未比对（待比对/比对中、字段清单为空又没有差异字段数）不算零变化结论。
        """
        if row.get("零差异") == "是":
            return True
        if row.get("零差异") == "否":
            return False
        if str(row.get("差异字段数") or "").strip().isdigit():
            return self._diff_count(row) == 0
        if self._split_fields(row.get("差异字段")):
            return False
        return str(row.get("status") or "") in ("已收敛",)

    def _comparison_pending(self, row: dict[str, Any]) -> bool:
        """既没有差异字段也没有差异字段数、状态仍未收敛，说明逐字段比对还没完成。"""
        if str(row.get("零差异") or "") in ("是", "否"):
            return False
        if str(row.get("差异字段数") or "").strip().isdigit():
            return False
        if self._split_fields(row.get("差异字段")):
            return False
        return str(row.get("status") or "") in ("待比对", "比对中")

    def diff_fields_of(self, diff_no: str) -> list[str]:
        """输出差异字段清单，供重解析时确定需要重新抽取的字段。"""
        for row in store.rows(self.MODULE):
            if row.get("差异编号") == diff_no:
                return [f.strip() for f in str(row.get("差异字段") or "").split(",") if f.strip()]
        return []

    def requires_split_reparse(self, diff_no: str) -> tuple[bool, int]:
        """判断该差异是否必须分批重解析，返回 (是否分批, 建议分批数)。"""
        for row in store.rows(self.MODULE):
            if row.get("差异编号") != diff_no:
                continue
            diff_count = self._diff_count(row)
            if diff_count <= self.MAX_SINGLE_PASS_FIELDS:
                return False, 1
            return True, -(-diff_count // self.MAX_SINGLE_PASS_FIELDS)
        return False, 1

    def unresolved_diffs(self) -> list[dict[str, Any]]:
        """列出偏离超限未收敛的差异条目，供治理看板消费。"""
        return [
            {
                "差异编号": r.get("差异编号"),
                "旧模板版本": r.get("旧模板版本"),
                "新模板版本": r.get("新模板版本"),
                "差异字段数": r.get("差异字段数"),
                "影响批次数": r.get("影响批次数"),
                "status": r.get("status"),
            }
            for r in store.rows(self.MODULE)
            if r.get("status") == "偏离超限"
        ]
