# -*- coding: utf-8 -*-
"""One optimization run, movable worktrees, one final selection."""

from pathlib import Path

from ..utils.io_utils import get_sync_path_lock
from .analyzer import digest, now, write_json
from .edit_scope import harness_path
from .versions import read_json


class OptimizationSession:
    def __init__(self, store, task, snapshot, targets, folder, binding):
        self.store, self.task = store, task
        self.snapshot, self.targets = snapshot, targets
        self.folder, self.binding = folder, binding
        self.lock = get_sync_path_lock(folder)
        self.row = None
        self.current = store.snapshot(
            task["snapshot_revision"],
            snapshot["targets"],
        )
        self.current["revision"] = task["snapshot_revision"]
        self.state = {
            "state": "running",
            "run_id": folder.name,
            "source": task["source"],
            "run_output": str(folder / "run.json"),
            "output": None,
            "candidate_id": None,
            "selected_candidate_id": None,
            "attempts": [],
            "decisions": [],
            "started_at": now(),
            "error": None,
        }
        self.persist()

    def persist(self):
        if self.row:
            self.state.update(
                {
                    k: self.row[k]
                    for k in (
                        "candidate_id",
                        "output",
                        "worktree",
                        "base_revision",
                        "candidate_revision",
                    )
                },
            )
        write_json(self.folder / "run.json", self.state)
        write_json(self.folder.parents[1] / "optimize_status.json", self.state)

    def log(self, action, reason, **details):
        self.state["decisions"].append(
            {
                "action": action,
                "reason": reason,
                "at": now(),
                **details,
            },
        )
        self.persist()

    def resolve(self, base):
        return (
            self.task["snapshot_revision"]
            if base == "snapshot"
            else self.store.resolve(base)
        )

    def compatible(self, revision):
        self.store.check_revision(
            self.task["snapshot_revision"],
            revision,
            self.task["base_targets"],
        )

    def draft(self):
        if not self.row:
            raise ValueError("请先用 start_candidate 选择起点并创建 worktree")
        if self.row["candidate_revision"] or self.row["status"] == "rejected":
            raise ValueError("版本已保存或放弃；请 start_candidate 创建新草稿")
        return self.row

    def can_leave(self):
        if (
            self.row
            and not self.row["candidate_revision"]
            and self.row["status"] != "rejected"
            and (self.store.diff(self.row) or self.row["status"] == "conflict")
        ):
            raise ValueError(
                "切换前请 checkpoint_candidate 保存或 reject_candidate 放弃草稿",
            )

    def refresh(self):
        # Keep the dict identity: all read/edit tools follow this worktree.
        self.current.clear()
        self.current.update(
            targets={
                key: dict(target)
                for key, target in self.snapshot["targets"].items()
            },
            revision=self.row["candidate_id"],
        )
        for target in self.current["targets"].values():
            path = harness_path(Path(self.row["worktree"]), target["path"])
            content = path.read_text(encoding="utf-8") if path.exists() else ""
            target.update(
                exists=path.exists(),
                content=content,
                content_hash=digest(content),
            )

    def start(self, base, reason):
        with self.lock:
            self.can_leave()
            revision = self.resolve(base)
            self.compatible(revision)
            previous = self.row
            self.row = self.store.create(revision, self.task, self.targets)
            self.row.update(
                run_output=self.state["run_output"],
                binding=self.binding,
            )
            self.store.save(self.row)
            self.state["attempts"].append(self.row["candidate_id"])
            if previous and not previous["candidate_revision"]:
                if previous["status"] != "rejected":
                    previous["status"] = "not_selected"
                    self.store.save(previous)
            self.refresh()
            self.log(
                "start",
                reason,
                base_revision=revision,
                candidate_id=self.row["candidate_id"],
            )
            return self.describe(self.row)

    def checkpoint(self, reason):
        with self.lock:
            self.store.commit(self.draft(), reason, status="checkpoint")
            self.log(
                "checkpoint",
                reason,
                candidate_id=self.row["candidate_id"],
            )
            return self.describe(self.row)

    def reject(self, reason):
        with self.lock:
            if not self.row:
                raise ValueError("尚无本次运行的尝试可放弃")
            self.store.reject(self.row)
            self.row["rejection_reason"] = reason
            self.store.save(self.row)
            self.log("reject", reason, candidate_id=self.row["candidate_id"])
            return self.describe(self.row)

    def integrate(self, action, source_id, reason):
        with self.lock:
            row = self.draft()
            source = self.store.record(source_id)
            if source is None:
                raise ValueError("找不到本 Harness 的来源候选")
            # Never inherit a source candidate's permissions.
            self.store.prepare_integration(row, source, action)
            self.refresh()
            self.log(
                action,
                reason,
                candidate_id=row["candidate_id"],
                source_candidate=source_id,
            )
            return self.describe(row)

    def describe(self, row):
        task = row["task"]
        binding = row.get("binding")
        manifest = Path(row["output"]).with_name("manifest.json")
        if binding is None and manifest.exists():
            binding = read_json(manifest)
        return {
            **{
                k: row.get(k)
                for k in (
                    "candidate_id",
                    "base_revision",
                    "candidate_revision",
                    "status",
                    "summary",
                    "created_at",
                    "error",
                    "rejection_reason",
                    "integration",
                )
            },
            "problem": task.get("proposal", {}).get("problem"),
            "targets": list(row["targets"]),
            "binding": binding,
            "limitations": row.get("result", {}).get("limitations", []),
            "worktree": row["worktree"] if row is self.row else None,
            "evaluation": "not_evaluated",
        }

    def finish(self, result):
        """Select one attempt; saved checkpoints are not implicitly winners."""
        with self.lock:
            selected = result.candidate_id or (
                self.row["candidate_id"] if self.row else None
            )
            row = (
                self.row
                if self.row and selected == self.row["candidate_id"]
                else self.store.record(selected)
            )
            if selected and row is None:
                raise ValueError("找不到所选候选")
            if result.candidate_id and row["status"] == "rejected":
                raise ValueError("不能选择已放弃的候选；请重新创建尝试")
            if row is not self.row:
                self.can_leave()
            if result.status == "modified":
                if (
                    not row
                    or selected not in self.state["attempts"]
                    or row["status"] == "rejected"
                ):
                    raise ValueError("modified 必须选择本次运行未放弃的候选")
                if not row["candidate_revision"]:
                    self.store.commit(row, result.summary)
                self.compatible(row["candidate_revision"])
                row.update(
                    status="ready_for_evaluation",
                    result=result.model_dump(),
                )
                self.store.save(row)
            elif row and row["candidate_revision"]:
                self.compatible(row["candidate_revision"])
            elif row and row["status"] != "rejected":
                if self.store.diff(row) or row["status"] == "conflict":
                    raise ValueError(
                        "返回未修改结果前，请先保存或明确放弃草稿",
                    )
                row.update(status=result.status, finished_at=now())
                self.store.save(row)
            self.state.update(
                state=(
                    "ready_for_evaluation"
                    if result.status == "modified"
                    else result.status
                ),
                selected_candidate_id=(
                    selected
                    if row
                    and row["candidate_revision"]
                    and row["status"] != "rejected"
                    and result.status in {"modified", "no_change"}
                    else None
                ),
                result=result.model_dump(),
                summary=result.summary,
                finished_at=now(),
            )
            if row is not self.row and self.row:
                if (
                    not self.row["candidate_revision"]
                    and self.row["status"] != "rejected"
                ):
                    self.row.update(status="not_selected", finished_at=now())
                    self.store.save(self.row)
            if row:
                self.row = row
            self.log(
                "select",
                result.summary,
                candidate_id=self.state["selected_candidate_id"],
            )

    def stop(self, state, error=None):
        if self.row and not self.row["candidate_revision"]:
            if self.row["status"] != "rejected":
                self.row.update(status=state, error=error, finished_at=now())
                self.store.save(self.row)
        self.state.update(state=state, error=error, finished_at=now())
        self.persist()
