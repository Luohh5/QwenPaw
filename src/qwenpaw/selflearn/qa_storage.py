"""One checkpoint database and one readable transcript per named operation."""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory
import zlib

from .analyzer import write_json as atomic_json

ACTIVE_STORE = ContextVar("qa_artifact_store", default=None)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, value)
    if store := ACTIVE_STORE.get():
        store.capture(path)


def safe_name(value):
    if not re.fullmatch(r"[\w][\w.\-]{0,180}", value, flags=re.UNICODE):
        raise ValueError(
            "名称只能包含文字、数字、下划线、点和短横线，不能包含路径"
        )
    return value


def period_name(source, explicit=None):
    if explicit:
        return safe_name(explicit)
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", Path(source).stem)
    if not dates and Path(source).suffix == ".jsonl":
        for line in Path(source).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            for key in (
                "received_at",
                "created_at",
                "timestamp",
                "create_time",
            ):
                value = str(row.get(key) or "")
                if re.match(r"^\d{4}-\d{2}-\d{2}", value):
                    dates.append(value[:10])
                    break
    if not dates:
        raise ValueError(
            "无法确定历史日期，请加 --name，例如 2026-08-20_2026-08-26"
        )
    for value in dates:
        datetime.strptime(value, "%Y-%m-%d")
    stem = Path(source).stem
    if re.fullmatch(
        r"\d{4}-\d{2}-\d{2}(?:_\d{4}-\d{2}-\d{2})?(?:_v\d+)?", stem
    ):
        return safe_name(stem)
    first, last = min(dates), max(dates)
    return first if first == last else first + "_" + last


@contextmanager
def workspace_lock(root):
    import fcntl

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".workflow.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("该工作区已有学习或评测任务执行中") from exc
        yield


class ArtifactStore:
    """Only durable logical artifacts are cached, never SDK context copies.

    Old pipeline code can operate in a temporary directory. Each JSON commit
    is immediately checkpointed; backend files are captured on progress and
    exit. The directory is discarded after the operation, including failures.
    """

    def __init__(self, path, transcript):
        self.path, self.transcript = Path(path), Path(transcript)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS files "
            "(path TEXT PRIMARY KEY, data BLOB NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS meta "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self.db.commit()
        self.work = None
        self._seen = {}

    def get(self, key, default=None):
        row = self.db.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute(
            "INSERT OR REPLACE INTO meta VALUES (?, ?)",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        self.db.commit()

    def put(self, relative, data):
        self.db.execute(
            "INSERT OR REPLACE INTO files VALUES (?, ?)",
            (str(relative), zlib.compress(data)),
        )
        self.db.commit()

    def capture(self, path):
        if self.work is None:
            return
        try:
            relative = Path(path).relative_to(self.work)
        except ValueError:
            return
        if "context" not in relative.parts and Path(path).suffix in {
            ".json",
            ".jsonl",
            ".txt",
            ".md",
        }:
            stat = Path(path).stat()
            signature = (stat.st_mtime_ns, stat.st_size)
            if self._seen.get(str(relative)) == signature:
                return
            data = Path(path).read_bytes()
            # Ignore incomplete concurrent backend writes; retry next tick.
            try:
                if Path(path).suffix == ".json":
                    json.loads(data)
                elif Path(path).suffix == ".jsonl":
                    for line in data.splitlines():
                        if line.strip():
                            json.loads(line)
            except (ValueError, UnicodeError):
                return
            self.put(relative, data)
            self._seen[str(relative)] = signature

    def checkpoint(self):
        if self.work is not None:
            for root, directories, files in os.walk(self.work):
                directories[:] = [d for d in directories if d != "context"]
                for name in files:
                    self.capture(Path(root) / name)

    def append(self, text):
        with self.transcript.open("a", encoding="utf-8") as stream:
            stream.write(text.rstrip() + "\n\n")
            stream.flush()

    @contextmanager
    def working(self):
        with TemporaryDirectory(prefix="qwenpaw-selflearn-") as temporary:
            self.work = Path(temporary).resolve()
            for relative, data in self.db.execute(
                "SELECT path, data FROM files"
            ):
                path = (self.work / relative).resolve()
                if not path.is_relative_to(self.work):
                    raise ValueError("检查点含非法路径")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(zlib.decompress(data))
            token = ACTIVE_STORE.set(self)
            try:
                yield self.work
            finally:
                self.checkpoint()
                ACTIVE_STORE.reset(token)
                self.work = None
                self.db.close()


def transcript_event(label, text, force=False, display=True):
    """Persist a readable event once and send the same text to Console."""
    from .qa_pipeline import PROGRESS

    store = ACTIVE_STORE.get()
    if not text or (store is None and not force):
        return
    value = f"### {label}\n\n{text}"
    if store is not None:
        store.append(value)
    if sink := PROGRESS.get():
        sink(
            json.dumps(
                {"event": "session", "text": value, "display": display},
                ensure_ascii=False,
            )
        )


def result_text(value):
    data = value.model_dump()
    lines = []
    for key, label in (
        ("question", "问题"),
        ("summary", "分析"),
        ("answer", "答案"),
        ("decision", "核验结论"),
        ("score", "分数"),
        ("reason", "原因"),
    ):
        if key in data:
            lines.append(
                f"**{label}**：{data[key] if data[key] is not None else '待核实'}"
            )
    for key, label in (
        ("findings", "发现的问题"),
        ("tasks", "改进建议"),
        ("deductions", "扣分依据"),
        ("uncertainties", "待核实"),
        ("missing_evidence", "缺少证据"),
    ):
        if data.get(key):
            content = json.dumps(data[key], ensure_ascii=False, indent=2)
            lines.append(f"**{label}**\n\n```json\n{content}\n```")
    return "\n\n".join(lines) or json.dumps(data, ensure_ascii=False, indent=2)
