"""Read-only, captured QwenPaw sources for FAQ research and verification."""

import asyncio
import hashlib
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx

from .analyzer import digest, now


def lines_page(text, start_line=1, search="", max_lines=100):
    lines = text.splitlines()
    start = max(1, start_line)
    selected = [
        (i, line)
        for i, line in enumerate(lines, 1)
        if i >= start and (not search or search.casefold() in line.casefold())
    ]
    page = selected[:max_lines]
    return {
        "total_lines": len(lines),
        "lines": page,
        "next_start_line": (
            selected[max_lines][0] if len(selected) > max_lines else None
        ),
    }


def git_read(root, *args):
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.rstrip("\n") if result.returncode == 0 else None


def default_repo():
    root = Path(__file__).resolve().parents[3]
    return root if (root / "pyproject.toml").is_file() else None


class ResearchSources:
    """Snapshot local text once; expose only these files to model tools."""

    def __init__(self, repo=None, knowledge=None, offline=False):
        self.repo = Path(repo).expanduser().resolve() if repo else None
        self.offline = offline
        self.files = {}
        self.loaded = {}
        self.read_ids = set()
        self.read_pages = {}
        revision = (
            git_read(self.repo, "rev-parse", "HEAD") if self.repo else None
        )
        remote = (
            git_read(self.repo, "remote", "get-url", "origin")
            if self.repo
            else None
        )
        official = bool(
            remote
            and re.search(
                r"(?:github\.com[:/])agentscope-ai/QwenPaw(?:\.git)?$",
                remote,
                re.I,
            )
        )
        remote_match = re.search(
            r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?$", remote or ""
        )
        remote_repo = remote_match[1] if remote_match else None
        tree = (
            git_read(self.repo, "ls-tree", "-r", "-z", revision)
            if revision
            else ""
        )
        tracked_blobs = {}
        for entry in (tree or "").split("\0"):
            if "\t" in entry:
                metadata, path = entry.split("\t", 1)
                tracked_blobs[path] = metadata.split()[-1]
        if self.repo:
            if not self.repo.is_dir():
                raise ValueError(f"源码目录不存在：{self.repo}")
            roots = [
                self.repo / p
                for p in (
                    "website/public/docs",
                    "website/public/release-notes",
                    "src/qwenpaw",
                    "pyproject.toml",
                )
            ]
            for root in roots:
                paths = (
                    [root]
                    if root.is_file()
                    else sorted(root.rglob("*")) if root.is_dir() else []
                )
                for path in paths:
                    rel = path.relative_to(self.repo).as_posix()
                    if (
                        path.suffix not in {".md", ".py", ".toml"}
                        or not path.is_file()
                    ):
                        continue
                    if "selflearn" in path.relative_to(self.repo).parts or any(
                        p.startswith(".") or p == "__pycache__"
                        for p in path.relative_to(self.repo).parts
                    ):
                        continue
                    if (
                        not path.resolve().is_relative_to(self.repo)
                        or path.is_symlink()
                        or path.stat().st_size > 1_000_000
                    ):
                        continue
                    raw = path.read_bytes()
                    text = raw.decode("utf-8")
                    hash_fn = (
                        hashlib.sha256
                        if len(revision or "") == 64
                        else hashlib.sha1
                    )
                    blob = hash_fn(
                        f"blob {len(raw)}\0".encode() + raw
                    ).hexdigest()
                    # Link clean, tracked files to their repository commit.
                    tracked = (
                        remote_repo
                        and revision
                        and tracked_blobs.get(rel) == blob
                    )
                    url = (
                        f"https://github.com/{remote_repo}/blob/"
                        f"{revision}/{quote(rel)}"
                        if tracked
                        else None
                    )
                    self.files[rel] = {
                        "text": text,
                        "url": url,
                        "path": rel,
                        "revision": revision,
                        "kind": "local_source",
                    }
        self.knowledge = None
        if knowledge:
            self.knowledge = Path(knowledge).expanduser().resolve()
            self.files["existing_knowledge.txt"] = {
                "text": self.knowledge.read_text(encoding="utf-8"),
                "url": None,
                "path": str(self.knowledge),
                "kind": "existing_knowledge",
            }
        self.binding = {
            "repo": str(self.repo) if self.repo else None,
            "revision": revision,
            "official_remote": official,
            "content_hash": digest(self.files),
            "file_count": len(self.files),
            "knowledge": str(self.knowledge) if self.knowledge else None,
            "offline": offline,
        }

    def search(self, query, scope="all", offset=0):
        terms = [t.casefold() for t in query.split() if t.strip()]
        if not terms:
            raise ValueError(
                "搜索词不能为空；中文可使用短关键词，代码用标识符"
            )
        hits = []
        for path, source in self.files.items():
            if scope == "knowledge" and source["kind"] != "existing_knowledge":
                continue
            if scope == "docs" and not path.endswith(".md"):
                continue
            text = source["text"]
            matching = [
                (i, line[:600])
                for i, line in enumerate(text.splitlines(), 1)
                if any(t in line.casefold() for t in terms)
            ]
            if matching or any(t in path.casefold() for t in terms):
                score = sum(
                    text.casefold().count(t)
                    + (20 if t in path.casefold() else 0)
                    for t in terms
                )
                hits.append((score, {"path": path, "matches": matching[:4]}))
        hits.sort(key=lambda x: (-x[0], x[1]["path"]))
        start = max(0, offset)
        return {
            "matches": [h[1] for h in hits[start : start + 15]],
            "total": len(hits),
            "next_offset": start + 15 if len(hits) > start + 15 else None,
        }

    def capture(self, source):
        key = digest(source)[:20]
        self.loaded.setdefault(key, dict(source, captured_at=now()))
        self.read_ids.add(key)
        return key

    def read_local(self, path, start_line=1, search=""):
        if path not in self.files:
            raise ValueError(
                "只允许读取 search_sources 中的源码、文档或指定的旧知识文件"
            )
        source = self.files[path]
        key = self.capture(source)
        return self.read_captured(key, start_line, search)

    def read_captured(self, source_id, start_line=1, search=""):
        source = self.loaded[source_id]
        self.read_ids.add(source_id)
        page = lines_page(source["text"], start_line, search)
        self.read_pages.setdefault(source_id, []).append(
            "\n".join(line for _, line in page["lines"])
        )
        return {
            "source_id": source_id,
            **{k: v for k, v in source.items() if k != "text"},
            **page,
        }

    @staticmethod
    def check_url(url):
        part = urlsplit(url)
        if (
            part.scheme != "https"
            or part.username
            or part.password
            or part.port not in (None, 443)
        ):
            raise ValueError("仅支持官方 HTTPS 来源")
        path = unquote(part.path)
        allowed = (
            part.hostname == "qwenpaw.agentscope.io"
            or part.hostname in {"github.com", "raw.githubusercontent.com"}
            and path.lower().startswith("/agentscope-ai/qwenpaw/")
            or part.hostname == "api.github.com"
            and (
                path.lower().startswith("/repos/agentscope-ai/qwenpaw/")
                or path == "/search/issues"
            )
        )
        if not allowed or any(p in {"..", "."} for p in path.split("/")):
            raise ValueError(
                "仅允许 QwenPaw 官方文档和 agentscope-ai/QwenPaw 来源"
            )

    async def get(self, url):
        if self.offline:
            raise ValueError("本轮 --offline，不能联网；请使用本地源码和文档")
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=False
        ) as client:
            for _ in range(5):
                self.check_url(url)
                async with client.stream(
                    "GET",
                    url,
                    headers={
                        "User-Agent": "QwenPaw-SelfLearn",
                        "Accept": (
                            "application/vnd.github+json, "
                            "text/plain, text/html"
                        ),
                    },
                ) as response:
                    if response.is_redirect:
                        url = urljoin(url, response.headers["location"])
                        continue
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 2_000_000:
                            raise ValueError(
                                "页面超过 2 MB，请读取更具体的页面"
                            )
                    return (
                        bytes(body).decode("utf-8"),
                        response.headers.get("content-type", ""),
                        url,
                    )
            raise ValueError("重定向次数过多")

    async def fetch(self, url, start_line=1, search=""):
        self.check_url(url)
        existing = next(
            (k for k, s in self.loaded.items() if s.get("url") == url), None
        )
        if existing:
            return self.read_captured(existing, start_line, search)
        fetch_url = url
        part = urlsplit(url)
        if part.hostname == "github.com":
            if "/blob/" in part.path:
                fetch_url = (
                    "https://raw.githubusercontent.com"
                    + part.path.replace("/blob/", "/", 1)
                )
            else:
                match = re.fullmatch(
                    r"/agentscope-ai/QwenPaw/(issues|pull)/(\d+)",
                    part.path,
                    re.I,
                )
                if match:
                    kind = "pulls" if match[1] == "pull" else "issues"
                    fetch_url = (
                        "https://api.github.com/repos/agentscope-ai/QwenPaw/"
                        f"{kind}/{match[2]}"
                    )
        text, content_type, final_url = await self.get(fetch_url)
        if "html" in content_type:
            import html2text

            converter = html2text.HTML2Text()
            converter.ignore_images, converter.body_width = True, 0
            text = converter.handle(text)
        elif "json" in content_type:
            text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        key = self.capture(
            {
                "text": text,
                "url": url,
                "fetched_url": final_url,
                "kind": "official_web",
            }
        )
        return self.read_captured(key, start_line, search)

    async def search_official(self, query):
        text, _, _ = await self.get(
            "https://api.github.com/search/issues?q="
            + quote(f"repo:agentscope-ai/QwenPaw {query}")
            + "&per_page=10"
        )
        value = json.loads(text)
        return {
            "total": value.get("total_count"),
            "items": [
                {
                    "title": i["title"],
                    "url": i["html_url"],
                    "snippet": (i.get("body") or "")[:600],
                }
                for i in value.get("items", [])
            ],
            "note": "仅供定位；用 fetch_official 读取原文，不把 issue 的观点当作已核实事实。",
        }

    def tools(self):
        from agentscope.tool import FunctionTool

        async def search_sources(
            query: str, scope: str = "all", offset: int = 0
        ) -> str:
            """Search frozen QwenPaw code/docs; scope: all, docs, knowledge.

            Space-separated literal keywords are OR-matched; page with offset.
            """
            return json.dumps(
                await asyncio.to_thread(self.search, query, scope, offset),
                ensure_ascii=False,
            )

        async def read_source(
            path: str, start_line: int = 1, search: str = ""
        ) -> str:
            """Read a path from search_sources, up to 100 lines.

            Capture citable source_id and text. Use next_start_line to page.
            """
            return json.dumps(
                self.read_local(path, start_line, search),
                ensure_ascii=False,
                indent=2,
            )

        async def fetch_official(
            url: str, start_line: int = 1, search: str = ""
        ) -> str:
            """Read an official URL and capture source_id.

            Supports QwenPaw docs, GitHub blobs, issues, PRs and repository
            GitHub API URLs. HTTP errors mean missing evidence.
            """
            return json.dumps(
                await self.fetch(url, start_line, search),
                ensure_ascii=False,
                indent=2,
            )

        async def search_official(query: str) -> str:
            """Search QwenPaw issues/PRs via public GitHub API, without a key.

            Search results are leads, not citable sources.
            """
            return json.dumps(
                await self.search_official(query), ensure_ascii=False
            )

        async def read_captured_source(
            source_id: str, start_line: int = 1, search: str = ""
        ) -> str:
            """Read captured sources for verification and exact quotes."""
            return json.dumps(
                self.read_captured(source_id, start_line, search),
                ensure_ascii=False,
                indent=2,
            )

        return [
            FunctionTool(fn, is_read_only=True)
            for fn in (
                search_sources,
                read_source,
                fetch_official,
                search_official,
                read_captured_source,
            )
        ]
