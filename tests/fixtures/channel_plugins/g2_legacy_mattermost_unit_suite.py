"""Original pure/HTTP business assertions on the real migrated platform.

Only httpx's external transport is controlled. Worker ownership and host
routing are separately exercised by the official process suites.
"""

from pathlib import Path
from collections import OrderedDict
import asyncio
import sys

import httpx
import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugins/channel/mattermost"))
from qwenpaw_channel_mattermost.platform import MattermostPlatform


class HTTPBoundary(httpx.AsyncClient):
    def __init__(self):
        self.responses = {}
        self._requests = []
        super().__init__(transport=httpx.MockTransport(self.respond))

    async def post(self, url, **kwargs):
        self._requests.append({"url": url, "kwargs": kwargs})
        return await super().post(url, **kwargs)

    def respond(self, request):
        return self.responses.get(
            (request.method, request.url.path), httpx.Response(404)
        )

    def expect(
        self,
        method,
        url,
        response_status=200,
        response_json=None,
        response_text="",
    ):
        self.responses[method, url] = httpx.Response(
            response_status,
            **(
                {"json": response_json}
                if response_json is not None
                else {"text": response_text}
            ),
        )

    def expect_get(self, **kwargs):
        self.expect("GET", **kwargs)

    def expect_post(self, **kwargs):
        self.expect("POST", **kwargs)


@pytest_asyncio.fixture
async def mock_http_client():
    async with HTTPBoundary() as client:
        yield client


async def unused_callback(_value):
    raise AssertionError("Pure platform test unexpectedly invoked an event")


@pytest.fixture
def mattermost_channel(tmp_path):
    return MattermostPlatform(
        {
            "url": "https://mattermost.example.com",
            "bot_token": "test_token",
            "workspace_dir": str(tmp_path),
            "media_dir": str(tmp_path / "media"),
        },
        unused_callback,
        unused_callback,
    )


def test_original_constructor_values_and_bounded_owned_structures(tmp_path):
    config = {
        "enabled": True,
        "url": "https://mm.example.com/",
        "bot_token": "my_token_123",
        "bot_prefix": "[Bot] ",
        "media_dir": str(tmp_path / "custom_media"),
        "show_typing": False,
        "thread_follow_without_mention": True,
    }
    platform = MattermostPlatform(config, unused_callback, unused_callback)
    assert platform._url == "https://mm.example.com"
    assert platform._bot_token == "my_token_123"
    assert platform.config["bot_prefix"] == "[Bot] "
    assert platform._media_dir == tmp_path / "custom_media"
    assert (
        platform._thread_follow is True
        and platform.config["show_typing"] is False
    )
    assert isinstance(platform._typing_tasks, dict)
    assert isinstance(platform._participated_threads, OrderedDict)
    assert isinstance(platform._seen_sessions, OrderedDict)
    assert platform._bot_id == platform._bot_username == ""
    assert (
        platform._http is None
    ), "SDK connection belongs to worker start, not static construction"


def test_original_default_media_directory_is_retained():
    from qwenpaw.constant import WORKING_DIR

    platform = MattermostPlatform({}, unused_callback, unused_callback)
    assert platform._media_dir == WORKING_DIR / "media" / "mattermost"


@pytest.mark.asyncio
async def test_original_partial_bot_identity_values_remain_available(tmp_path):
    sys.path.insert(0, str(ROOT / "tests/integration"))
    from mock_mattermost import MockMattermost

    service = MockMattermost()
    service.responses["/api/v4/users/me"] = (200, {"id": "user_123"})
    service.start()
    states = []

    async def state(value):
        states.append(value)

    platform = MattermostPlatform(
        {
            "url": service.url,
            "bot_token": "local-token",
            "workspace_dir": str(tmp_path),
        },
        unused_callback,
        state,
    )
    try:
        await platform.start()
        async with asyncio.timeout(8):
            while "connected" not in states:
                await asyncio.sleep(0.01)
        assert platform._bot_id == "user_123" and platform._bot_username == ""
        assert platform.connected
    finally:
        await platform.stop()
        service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("source_type", ["data", "path", "file_url"])
async def test_file_upload_uses_display_name(
    mattermost_channel, mock_http_client, tmp_path, source_type
):
    local = tmp_path / "local.pdf"
    local.write_bytes(b"pdf-data")
    sources = {
        "data": "data:application/pdf;base64,cGRmLWRhdGE=",
        "path": str(local),
        "file_url": local.as_uri(),
    }
    mock_http_client.expect_post(
        url="/api/v4/files",
        response_status=201,
        response_json={"file_infos": [{"id": "file-id"}]},
    )
    mock_http_client.expect_post(
        url="/api/v4/posts",
        response_status=201,
        response_json={"id": "post-id"},
    )
    mattermost_channel._http = mock_http_client
    await mattermost_channel.send_parts(
        "recipient",
        "",
        [
            {
                "type": "file",
                "file_url": sources[source_type],
                "filename": "report.pdf",
            }
        ],
    )
    upload = mock_http_client._requests[0]["kwargs"]["files"]["files"]
    assert upload[0] == (
        "report.pdf" if source_type == "data" else "local.pdf"
    )
    assert upload[1].closed
    assert mock_http_client._requests[1]["kwargs"]["json"] == {
        "channel_id": "recipient",
        "message": "",
        "root_id": "",
        "file_ids": ["file-id"],
    }
    assert local.read_bytes() == b"pdf-data"
    if source_type == "data":
        assert not list(mattermost_channel._media_dir.iterdir())
    else:
        assert not mattermost_channel._media_dir.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("retry", [False, True])
async def test_actual_websocket_close_error_propagates_and_keeps_failed_resource(
    tmp_path, monkeypatch, retry
):
    sys.path.insert(0, str(ROOT / "tests/integration"))
    from mock_mattermost import MockMattermost

    service = MockMattermost()
    service.responses["/api/v4/users/me"] = (
        200,
        {"id": "bot_user_123", "username": "testbot"},
    )
    service.start()
    states = []

    async def state(value):
        states.append(value)

    platform = MattermostPlatform(
        {
            "url": service.url,
            "bot_token": "local-token",
            "workspace_dir": str(tmp_path),
        },
        unused_callback,
        state,
    )
    socket = None
    try:
        await platform.start()
        async with asyncio.timeout(8):
            while "connected" not in states:
                await asyncio.sleep(0.01)
        socket, task = platform._ws, platform._task
        assert (
            platform._bot_id == "bot_user_123"
            and platform._bot_username == "testbot"
        )
        original_close = type(socket).close
        entered = tmp_path / "actual-sdk-close-entered"
        fault = tmp_path / "sdk-close-fault"
        fault.touch()
        attempts = []

        async def close_failure(connection, *args, **kwargs):
            if connection is socket:
                entered.write_text(str(id(connection)))
                attempts.append(id(connection))
                if fault.exists():
                    raise OSError("controlled actual SDK close failure")
            return await original_close(connection, *args, **kwargs)

        with monkeypatch.context() as sdk:
            sdk.setattr(type(socket), "close", close_failure)
            with pytest.raises(
                OSError, match="controlled actual SDK close failure"
            ):
                await platform.stop()
            assert entered.read_text() == str(id(socket))
            assert platform._ws is socket and platform._task is task
            assert task.done() and isinstance(task.exception(), OSError)
            if retry:
                # Persistent failure must still propagate on another attempt.
                with pytest.raises(
                    OSError, match="controlled actual SDK close failure"
                ):
                    await platform.stop()
                assert attempts == [id(socket), id(socket)]
                assert platform._ws is socket and platform._task is task
                fault.unlink()
                await platform.stop()
                assert attempts == [id(socket), id(socket), id(socket)]
                assert (
                    platform._ws is None
                    and platform._task is None
                    and platform._http is None
                )
                assert not platform.connected
                assert not service.has_connection
    finally:
        if socket is not None:
            await original_close(socket)
        if platform._http is not None:
            await platform._http.aclose()
        if platform._task is not None:
            platform._task.cancel()
            await asyncio.gather(platform._task, return_exceptions=True)
        service.stop()


class TestMattermostFetchHistory:
    """Tests for history fetching methods."""

    @pytest.mark.asyncio
    async def test_fetch_thread_history_success(
        self,
        mattermost_channel,
        mock_http_client,
    ):
        """Should fetch and format thread history."""
        mock_http_client.expect_get(
            url="/api/v4/posts/root_123/thread",
            response_status=200,
            response_json={
                "order": ["post1", "post2", "post3"],
                "posts": {
                    "post1": {
                        "id": "post1",
                        "user_id": "user_abc",
                        "message": "First message",
                        "create_at": 1000,
                    },
                    "post2": {
                        "id": "post2",
                        "user_id": "user_def",
                        "message": "Second message",
                        "create_at": 2000,
                    },
                    "post3": {
                        "id": "post3",
                        "user_id": "user_abc",
                        "message": "Third message",
                        "create_at": 3000,
                    },
                },
            },
        )
        mattermost_channel._http = mock_http_client
        mattermost_channel._bot_id = "bot_123"

        result = await mattermost_channel._fetch_thread_history(
            root_id="root_123",
            triggering_post_id="post3",
        )

        assert "[Thread history]" in result
        assert "User: First message" in result
        assert "User: Second message" in result

    @pytest.mark.asyncio
    async def test_fetch_thread_history_api_error(
        self,
        mattermost_channel,
        mock_http_client,
    ):
        """Should return empty string on API error."""
        mock_http_client.expect_get(
            url="/api/v4/posts/root_123/thread",
            response_status=404,
        )
        mattermost_channel._http = mock_http_client

        result = await mattermost_channel._fetch_thread_history(
            root_id="root_123",
        )

        assert result == ""

    @pytest.mark.asyncio
    async def test_fetch_channel_history_success(
        self,
        mattermost_channel,
        mock_http_client,
    ):
        """Should fetch and format channel history."""
        mock_http_client.expect_get(
            url="/api/v4/channels/channel_123/posts",
            response_status=200,
            response_json={
                "order": ["post1", "post2"],
                "posts": {
                    "post1": {
                        "id": "post1",
                        "user_id": "user_abc",
                        "message": "Hello",
                    },
                    "post2": {
                        "id": "post2",
                        "user_id": "bot_123",
                        "message": "Hi there",
                    },
                },
            },
        )
        mattermost_channel._http = mock_http_client
        mattermost_channel._bot_id = "bot_123"

        result = await mattermost_channel._fetch_channel_history(
            mm_channel_id="channel_123",
            per_page=10,
        )

        assert "[Recent 10 DM context messages]" in result
        assert "User: Hello" in result
        assert "Bot: Hi there" in result

    @pytest.mark.asyncio
    async def test_fetch_channel_history_api_error(
        self,
        mattermost_channel,
        mock_http_client,
    ):
        """Should return empty string on API error."""
        mock_http_client.expect_get(
            url="/api/v4/channels/channel_123/posts",
            response_status=403,
        )
        mattermost_channel._http = mock_http_client

        result = await mattermost_channel._fetch_channel_history(
            mm_channel_id="channel_123",
        )

        assert result == ""


class TestMattermostFileOperations:
    """Tests for file download and upload."""

    @pytest.mark.asyncio
    async def test_upload_file_success(
        self,
        mattermost_channel,
        mock_http_client,
        tmp_path: Path,
    ):
        """Should upload file and return file_id."""
        test_file = tmp_path / "upload.txt"
        test_file.write_text("test content")

        mock_http_client.expect_post(
            url="/api/v4/files",
            response_status=201,
            response_json={"file_infos": [{"id": "file_abc"}]},
        )
        mattermost_channel._http = mock_http_client

        result = await mattermost_channel._upload_file(
            mm_channel_id="channel_123",
            local_path=str(test_file),
        )

        assert result == "file_abc"

    @pytest.mark.asyncio
    async def test_upload_file_not_found(
        self,
        mattermost_channel,
        mock_http_client,
    ):
        """Should return None when file not found."""
        mattermost_channel._http = mock_http_client

        result = await mattermost_channel._upload_file(
            mm_channel_id="channel_123",
            local_path="/nonexistent/file.txt",
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_upload_file_api_error(
        self,
        mattermost_channel,
        mock_http_client,
        tmp_path: Path,
    ):
        """Should return None on API error."""
        test_file = tmp_path / "upload.txt"
        test_file.write_text("test")

        mock_http_client.expect_post(
            url="/api/v4/files",
            response_status=500,
            response_text="Server Error",
        )
        mattermost_channel._http = mock_http_client

        result = await mattermost_channel._upload_file(
            mm_channel_id="channel_123",
            local_path=str(test_file),
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_download_file_success(
        self,
        mattermost_channel,
        mock_http_client,
    ):
        """Should download file and return local path."""
        mock_http_client.expect_get(
            url="/api/v4/files/file_123",
            response_status=200,
            response_text="file content",
        )
        mattermost_channel._http = mock_http_client

        result = await mattermost_channel._download_file(
            file_id="file_123",
            filename_hint="document.pdf",
        )

        assert result is not None
        assert result.endswith(".pdf")

    @pytest.mark.asyncio
    async def test_download_file_api_error(
        self,
        mattermost_channel,
        mock_http_client,
    ):
        """Should return None on API error."""
        mock_http_client.expect_get(
            url="/api/v4/files/file_123",
            response_status=404,
        )
        mattermost_channel._http = mock_http_client

        result = await mattermost_channel._download_file(
            file_id="file_123",
        )

        assert result is None


class TestMattermostIsTriggered:
    """Tests for _is_triggered method."""

    def test_is_triggered_dm(self, mattermost_channel):
        """Should trigger on DM messages."""
        mattermost_channel._bot_id = "bot_123"
        mattermost_channel._bot_username = "testbot"

        post = {
            "user_id": "user_abc",
            "message": "Hello",
            "root_id": "",
        }

        result = mattermost_channel._is_triggered(post, channel_type="D")

        assert result is True

    def test_is_triggered_mention(self, mattermost_channel):
        """Should trigger on bot mention."""
        mattermost_channel._bot_id = "bot_123"
        mattermost_channel._bot_username = "testbot"

        post = {
            "user_id": "user_abc",
            "message": "Hey @testbot help me",
            "root_id": "",
        }

        result = mattermost_channel._is_triggered(post, channel_type="O")

        assert result is True

    def test_is_triggered_thread_follow(self, mattermost_channel):
        """Should trigger on thread participation."""
        mattermost_channel._bot_id = "bot_123"
        mattermost_channel._thread_follow = True
        mattermost_channel._participated_threads["thread_abc"] = None

        post = {
            "user_id": "user_abc",
            "message": "Hello",
            "root_id": "thread_abc",
        }

        result = mattermost_channel._is_triggered(post, channel_type="O")

        assert result is True

    def test_is_triggered_skip_bot_message(self, mattermost_channel):
        """Should not trigger on bot's own messages."""
        mattermost_channel._bot_id = "bot_123"

        post = {
            "user_id": "bot_123",
            "message": "Hello",
        }

        result = mattermost_channel._is_triggered(post, channel_type="O")

        assert result is False

    def test_is_triggered_no_mention_not_dm(self, mattermost_channel):
        """Should not trigger without mention in non-DM."""
        mattermost_channel._bot_id = "bot_123"
        mattermost_channel._bot_username = "testbot"

        post = {
            "user_id": "user_abc",
            "message": "Hello everyone",
            "root_id": "",
        }

        result = mattermost_channel._is_triggered(post, channel_type="O")

        assert result is False


class TestMattermostChunkText:
    """Tests for _chunk_text method."""

    def test_chunk_text_short(self, mattermost_channel):
        """Should not chunk short text."""
        text = "Short message"

        chunks = mattermost_channel._chunk_text(text)

        assert len(chunks) == 1
        assert chunks[0] == text

    def test_chunk_text_empty(self, mattermost_channel):
        """Should return empty list for empty text."""
        chunks = mattermost_channel._chunk_text("")

        assert chunks == []

    def test_chunk_text_long(self, mattermost_channel):
        """Should chunk long text at boundaries."""
        # Create text longer than MATTERMOST_POST_CHUNK_SIZE
        from qwenpaw_channel_mattermost.platform import (
            MATTERMOST_POST_CHUNK_SIZE,
        )

        text = "A" * (MATTERMOST_POST_CHUNK_SIZE + 1000)

        chunks = mattermost_channel._chunk_text(text)

        assert len(chunks) > 1
        # Each chunk should be within limit
        for chunk in chunks:
            assert len(chunk) <= MATTERMOST_POST_CHUNK_SIZE

    def test_chunk_text_breaks_at_newline(self, mattermost_channel):
        """Should prefer to break at newlines."""
        from qwenpaw_channel_mattermost.platform import (
            MATTERMOST_POST_CHUNK_SIZE,
        )

        # Create text with newlines over the limit
        line = "A" * 100 + "\n"
        repeats = (MATTERMOST_POST_CHUNK_SIZE // 100) + 5
        text = line * repeats

        chunks = mattermost_channel._chunk_text(text)

        assert len(chunks) >= 1


class TestMattermostEdgeCases:
    def test_get_thread_target_order_with_bot_replies(
        self,
        mattermost_channel,
    ):
        """Should correctly identify gap when bot has replied."""
        mattermost_channel._bot_id = "bot_123"

        order = ["post1", "post2", "post3", "post4"]
        posts = {
            "post1": {"user_id": "user_abc", "message": "Hello"},
            "post2": {"user_id": "bot_123", "message": "Bot reply"},
            "post3": {"user_id": "user_abc", "message": "Follow up"},
            "post4": {"user_id": "user_abc", "message": "Another message"},
        }

        target_order, label = mattermost_channel._get_thread_target_order(
            order,
            posts,
            last_bot_idx=1,
        )

        assert len(target_order) == 2
        assert target_order == ["post3", "post4"]
        assert "supplement" in label

    def test_get_thread_target_order_first_time(self, mattermost_channel):
        """Should return full history when bot hasn't replied."""
        order = ["post1", "post2"]
        posts = {
            "post1": {"user_id": "user_abc", "message": "Hello"},
            "post2": {"user_id": "user_def", "message": "Hi"},
        }

        target_order, label = mattermost_channel._get_thread_target_order(
            order,
            posts,
            last_bot_idx=-1,
        )

        assert target_order == order
        assert "history" in label
