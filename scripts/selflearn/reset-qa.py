#!/usr/bin/env python3
"""Archive one unfinished QA batch so the same raw JSONL can start fresh."""

import argparse
from datetime import datetime, timezone
from pathlib import Path
import shutil

from contextlib import contextmanager
import fcntl


@contextmanager
def workspace_lock(root, create):
    path = root / ".workflow.lock"
    if not create and not path.exists():
        yield
        return
    if create:
        root.mkdir(parents=True, exist_ok=True)
    with path.open("a+b" if create else "rb") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError(
                "工作区还有任务在执行，请先 /selflearn stop"
            ) from exc
        yield


def reset(source, apply=False):
    source = Path(source).expanduser().absolute()
    folder = source.parent
    if (
        source.is_symlink()
        or not source.is_file()
        or source.name != folder.name + ".jsonl"
        or folder.parent.name != "history_jsonl"
        or folder.parent.parent.name != "analyze"
    ):
        raise ValueError(
            "输入必须是 analyze/history_jsonl/A/A.jsonl 的原始文件"
        )
    workspace = folder.parent.parent.parent
    name = folder.name
    with workspace_lock(workspace / "selflearn", create=apply):
        if (folder / "round.json").exists():
            raise ValueError(
                "此批已有产出关联记录；为保留后续评测溯源，请用 --name 新名称重跑"
            )
        paths = [p for p in folder.iterdir() if p != source]
        paths += [
            workspace / "analyze/.state" / (name + suffix)
            for suffix in (".sqlite", "-dataset.sqlite", "-train.sqlite")
        ]
        paths += [workspace / "analyze/sessions" / (name + ".md")]
        output = workspace / "analyze/txt" / (name + ".txt")
        if output.exists():
            raise ValueError(
                "已存在 TXT；请用 --name 新名称重跑，避免破坏评测溯源"
            )
        paths = [p for p in paths if p.exists()]
        if any(
            p.is_symlink()
            or not p.resolve().is_relative_to(workspace.resolve())
            for p in paths
        ):
            raise ValueError("存档路径包含符号链接或越界路径，停止操作")
        if not apply or not paths:
            return {"apply": False, "files": [str(p) for p in paths]}
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        archive = workspace / "analyze/.archive" / name / stamp
        archive.mkdir(parents=True)
        backup = archive / source.relative_to(workspace)
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, backup)
        if backup.read_bytes() != source.read_bytes():
            raise ValueError("原始历史备份校验失败，未移动其他文件")
        moved = []
        try:
            for path in paths:
                target = archive / path.relative_to(workspace)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(target))
                moved.append((path, target))
        except BaseException:
            for path, target in reversed(moved):
                shutil.move(str(target), str(path))
            raise
        return {"apply": True, "archive": str(archive), "source": str(source)}


if __name__ == "__main__":
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument(
        "--apply", action="store_true", help="执行备份迁移；默认仅列出范围"
    )
    args = parser.parse_args()
    print(
        json.dumps(
            reset(args.source, args.apply), ensure_ascii=False, indent=2
        )
    )
