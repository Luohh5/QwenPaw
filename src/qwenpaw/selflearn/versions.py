# -*- coding: utf-8 -*-
"""Local Harness history, with controlled operations and no release tools."""

import json
import os
import re
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from ..utils.io_utils import get_sync_path_lock
from .analyzer import digest, now, write_json
from .edit_scope import check_edit, harness_path


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class HarnessStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.repo = self.root / "repo.git"

    def git(self, *args, cwd=None, index=None, input_text=None, check=True):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_LITERAL_PATHSPECS="1",
            GIT_AUTHOR_NAME="QwenPaw",
            GIT_AUTHOR_EMAIL="selflearn@localhost",
            GIT_COMMITTER_NAME="QwenPaw",
            GIT_COMMITTER_EMAIL="selflearn@localhost",
        )
        location = ["-C", str(cwd)] if cwd else [f"--git-dir={self.repo}"]
        if index is not None:
            env["GIT_INDEX_FILE"] = str(index)
            location = [f"--git-dir={self.repo}", f"--work-tree={cwd}"]
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=" + os.devnull,
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.autocrlf=false",
                "-c",
                "core.quotePath=false",
                *location,
                *args,
            ],
            env=env,
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
        )
        if check and result.returncode:
            raise ValueError(result.stderr.strip() or result.stdout.strip())
        return result.stdout if check else result

    @classmethod
    def for_snapshot(cls, root: Path, snapshot: dict):
        identity = digest([snapshot["root"], snapshot["name"]])[:12]
        return cls(root / "harnesses" / identity)

    def import_snapshot(self, snapshot: dict) -> str:
        """Import exact declared files, without touching the source project."""
        with get_sync_path_lock(self.root):
            self.root.mkdir(parents=True, exist_ok=True)
            if not self.repo.exists():
                self.git("init", "--bare", str(self.repo))
                write_json(
                    self.root / "harness.json",
                    {
                        "id": self.root.name,
                        "name": snapshot["name"],
                        "source_root": snapshot["root"],
                    },
                )
            ref = "refs/heads/baseline/" + digest(snapshot)
            found = self.git("rev-parse", "--verify", ref, check=False)
            if found.returncode == 0:
                return found.stdout.strip()
            with TemporaryDirectory(dir=self.root, prefix="import-") as name:
                folder = Path(name)
                files = {
                    t["path"]: t["content"]
                    for t in snapshot["targets"].values()
                    if t["exists"]
                }
                for relative, content in files.items():
                    path = harness_path(folder, relative)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8")
                index = folder / ".import-index"
                if files:
                    self.git(
                        "add",
                        "-f",
                        "--",
                        *files,
                        cwd=folder,
                        index=index,
                    )
                else:
                    self.git("read-tree", "--empty", cwd=folder, index=index)
                tree = self.git("write-tree", cwd=folder, index=index).strip()
                revision = self.git(
                    "commit-tree",
                    tree,
                    "-m",
                    "Import Harness snapshot",
                ).strip()
                self.git("update-ref", ref, revision)
            return revision

    def resolve(self, value: str) -> str:
        candidate = self.root / "experiments" / value / "candidate.json"
        if re.fullmatch(r"[a-f0-9]{12}", value) and candidate.is_file():
            value = read_json(candidate).get("candidate_revision") or ""
        if not re.fullmatch(r"[a-f0-9]{40}", value):
            raise ValueError(
                "起点必须为本 Harness 的候选 ID 或完整 commit SHA",
            )
        return self.git("rev-parse", "--verify", value + "^{commit}").strip()

    def records(self):
        return sorted(
            (
                read_json(p)
                for p in self.root.glob("experiments/*/candidate.json")
            ),
            key=lambda row: row["created_at"],
            reverse=True,
        )

    def record(self, candidate_id):
        return next(
            (
                row
                for row in self.records()
                if row["candidate_id"] == candidate_id
            ),
            None,
        )

    def check_revision(self, baseline, revision, targets):
        """A historical base cannot smuggle protected code into a new run."""
        allowed = {t["path"]: t for t in targets.values()}
        for line in self.git(
            "diff",
            "--raw",
            "--no-renames",
            baseline,
            revision,
        ).splitlines():
            metadata, relative = line.split("\t", 1)
            old_mode, new_mode = metadata.lstrip(":").split()[:2]
            if (
                relative not in allowed
                or new_mode != "100644"
                or old_mode not in {"000000", new_mode}
            ):
                raise ValueError(f"历史版本包含当前范围外的改动：{relative}")
            check_edit(
                allowed[relative],
                self.content(baseline, relative),
                self.content(revision, relative),
            )

    def create(self, base: str, task: dict, targets: dict) -> dict:
        with get_sync_path_lock(self.root):
            candidate_id = uuid4().hex[:12]
            worktree = self.root / "worktrees" / candidate_id
            worktree.parent.mkdir(parents=True, exist_ok=True)
            self.git(
                "worktree",
                "add",
                "-b",
                "candidate/" + candidate_id,
                str(worktree),
                base,
            )
            folder = self.root / "experiments" / candidate_id
            folder.mkdir(parents=True)
            row = {
                "schema_version": 1,
                "candidate_id": candidate_id,
                "harness_id": self.root.name,
                "base_revision": base,
                "candidate_revision": None,
                "status": "running",
                "created_at": now(),
                "worktree": str(worktree),
                "output": str(folder / "candidate.json"),
                "task": task,
                "targets": targets,
                "checks": None,
                "error": None,
            }
            self.save(row)
            return row

    def save(self, row):
        write_json(Path(row["output"]), row)

    def content(self, revision: str, relative: str) -> str:
        # Only callers with a declared target use this; absence is meaningful.
        result = self.git("show", f"{revision}:{relative}", check=False)
        return result.stdout if result.returncode == 0 else ""

    def snapshot(self, revision: str, targets: dict) -> dict:
        paths = set(
            self.git("ls-tree", "-r", "--name-only", revision).splitlines(),
        )
        contents = {
            path: self.content(revision, path)
            for path in {t["path"] for t in targets.values()}
        }
        return {
            "targets": {
                key: dict(
                    target,
                    exists=target["path"] in paths,
                    content=contents[target["path"]],
                    content_hash=digest(contents[target["path"]]),
                )
                for key, target in targets.items()
            },
        }

    def check(self, row) -> dict:
        worktree = Path(row["worktree"])
        tracked = self.git(
            "diff",
            "--name-only",
            row["base_revision"],
            cwd=worktree,
        )
        untracked = self.git(
            "ls-files",
            "--others",
            "--exclude-standard",
            cwd=worktree,
        )
        paths = sorted(set((tracked + untracked).splitlines()))
        allowed = {t["path"]: t for t in row["targets"].values()}
        for relative in paths:
            if relative not in allowed:
                raise ValueError(f"未授权的文件改动：{relative}")
            path = harness_path(worktree, relative)
            if not path.is_file():
                raise ValueError("初版不允许删除 Harness 文件")
            check_edit(
                allowed[relative],
                self.content(row["base_revision"], relative),
                path.read_text(encoding="utf-8"),
            )
        modes = self.git(
            "diff",
            "--summary",
            row["base_revision"],
            cwd=worktree,
        )
        if "mode change" in modes:
            raise ValueError("不能修改文件执行权限")
        if self.git("ls-files", "--unmerged", cwd=worktree):
            for relative in paths:
                if re.search(
                    r"^(<<<<<<< |=======$|>>>>>>> )",
                    harness_path(
                        worktree,
                        relative,
                    ).read_text(encoding="utf-8"),
                    re.M,
                ):
                    raise ValueError(
                        "仍有未解决的合并冲突；请编辑文件后再 save",
                    )
        self.git("diff", "--check", row["base_revision"], cwd=worktree)
        return {
            "passed": True,
            "changed_files": paths,
            "scope": (
                "edit boundary and syntax only; performance not evaluated"
            ),
        }

    def commit(self, row, summary: str, status="ready_for_evaluation") -> dict:
        with get_sync_path_lock(self.root):
            if row["candidate_revision"]:
                raise ValueError(
                    "候选已保存；请从该版本创建新候选，不能改写历史",
                )
            checks = self.check(row)
            if not checks["changed_files"]:
                raise ValueError("没有可保存的改动")
            worktree = Path(row["worktree"])
            self.git("add", "--", *checks["changed_files"], cwd=worktree)
            self.git("commit", "-m", summary, cwd=worktree)
            row.update(
                candidate_revision=self.git(
                    "rev-parse",
                    "HEAD",
                    cwd=worktree,
                ).strip(),
                status=status,
                checks=checks,
                summary=summary,
                error=None,
                finished_at=now(),
            )
            self.save(row)
            return row

    def diff(self, row, other=None) -> str:
        if other:
            if not row["candidate_revision"]:
                raise ValueError("比较两个版本前请先保存候选")
            return self.git(
                "diff",
                row["candidate_revision"],
                self.resolve(other),
            )
        if row["candidate_revision"]:
            return self.git(
                "diff",
                row["base_revision"],
                row["candidate_revision"],
            )
        worktree = Path(row["worktree"])
        text = self.git("diff", row["base_revision"], cwd=worktree)
        for relative in self.git(
            "ls-files",
            "--others",
            "--exclude-standard",
            cwd=worktree,
        ).splitlines():
            result = self.git(
                "diff",
                "--no-index",
                "--",
                os.devnull,
                str(harness_path(worktree, relative)),
                check=False,
            )
            text += result.stdout
        return text

    def prepare_integration(self, row, source, operation):
        """Apply into an empty draft; keep conflicts editable, never commit."""
        if operation not in {"merge", "pick", "revert"}:
            raise ValueError("operation 必须是 merge、pick 或 revert")
        if row["candidate_revision"] or self.diff(row):
            raise ValueError(
                "请在无改动的新 worktree 中合并；先保存或放弃草稿",
            )
        revision = self.resolve(source["candidate_id"])
        row["integration"] = {
            "operation": operation,
            "source_candidate": source["candidate_id"],
        }
        try:
            worktree = Path(row["worktree"])
            if operation == "merge":
                self.git(
                    "merge",
                    "--no-commit",
                    "--no-ff",
                    revision,
                    cwd=worktree,
                )
            else:
                patch = self.git("diff", source["base_revision"], revision)
                self.git(
                    "apply",
                    *(["--reverse"] if operation == "revert" else []),
                    "--index",
                    "--3way",
                    "-",
                    input_text=patch,
                    cwd=worktree,
                )
            self.check(row)
            row.update(status="running", error=None)
        except ValueError as exc:
            row.update(status="conflict", error=str(exc))
        self.save(row)
        return row

    def integrate(
        self,
        first: dict,
        second: dict | None = None,
        operation="merge",
    ) -> dict:
        """Merge or selectively revert; never rewrite saved commits."""
        base = self.resolve(first["candidate_id"])
        source = second or first
        targets = dict(first["targets"], **source["targets"])
        row = self.create(
            base,
            {
                "operation": operation,
                "source_candidates": [
                    first["candidate_id"],
                    source["candidate_id"],
                ],
            },
            targets,
        )
        self.prepare_integration(row, source, operation)
        if row["status"] == "conflict":
            return row
        if not self.check(row)["changed_files"]:
            row.update(status="no_change", finished_at=now())
            self.save(row)
            return row
        return self.commit(row, operation + " " + source["candidate_id"])

    def reject(self, row):
        row.update(status="rejected", finished_at=now())
        self.save(row)
        return row

    def clean(self, row):
        # Git refuses unsaved changes; keep the branch and experiment.
        if row["status"] == "running":
            raise ValueError("请先停止正在运行的候选")
        worktree = Path(row["worktree"])
        if worktree.exists():
            self.git("worktree", "remove", str(worktree))
        return row


def find_candidate(root: Path, candidate_id: str):
    if not re.fullmatch(r"[a-f0-9]{12}", candidate_id):
        raise ValueError("候选 ID 必须是 12 位十六进制字符")
    paths = list(
        (root / "harnesses").glob(
            f"*/experiments/{candidate_id}/candidate.json",
        ),
    )
    if len(paths) != 1:
        raise ValueError(f"找不到唯一的候选：{candidate_id}")
    return HarnessStore(paths[0].parents[2]), read_json(paths[0])
