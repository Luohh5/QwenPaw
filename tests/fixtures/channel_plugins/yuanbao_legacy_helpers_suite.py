"""Mechanical import retarget of complete frozen Yuanbao pure business oracles."""
from __future__ import annotations

import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch
import nturl2path
import pytest

from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "plugins/channel/yuanbao"))

from qwenpaw_channel_yuanbao import media as yuanbao_media
from qwenpaw.app.channels import utils as channel_utils

@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (r"C:\media\report.pdf", r"C:\media\report.pdf"),
        ("file:///C:/media/report.pdf", r"C:\media\report.pdf"),
        ("file://C:/media/report.pdf", r"C:\media\report.pdf"),
        ("file:///C:/media/my%20report.pdf", r"C:\media\my report.pdf"),
        (r"\\server\share\report.pdf", r"\\server\share\report.pdf"),
        ("file://server/share/report.pdf", r"\\server\share\report.pdf"),
    ],
)
def test_resolve_windows_media_paths(source, expected):
    """Yuanbao uses Windows drive and UNC conversion on every test host."""
    with (
        patch.object(channel_utils, "os", SimpleNamespace(name="nt")),
        patch.object(channel_utils, "url2pathname", nturl2path.url2pathname),
    ):
        assert yuanbao_media._resolve_local_path(source) == expected


@pytest.mark.parametrize("source_type", ["data", "path", "file_url"])
async def test_media_source_reaches_cos_upload(tmp_path, source_type):
    """Data URLs and existing local references reach the same upload flow."""
    local_file = tmp_path / "report.pdf"
    local_file.write_bytes(b"pdf-data")
    sources = {
        "data": "data:application/pdf;base64,cGRmLWRhdGE=",
        "path": str(local_file),
        "file_url": local_file.as_uri(),
    }
    session = MagicMock()
    config = MagicMock()
    with (
        patch.object(
            yuanbao_media,
            "get_upload_info",
            new=AsyncMock(return_value=config),
        ) as get_info,
        patch.object(
            yuanbao_media,
            "upload_to_cos",
            new=AsyncMock(return_value="https://cdn.example.com/file.pdf"),
        ) as upload,
    ):
        result = await yuanbao_media.download_and_upload_media(
            sources[source_type],
            session,
            "yuanbao.tencent.com",
            {},
        )

    expected_name = "file.pdf" if source_type == "data" else "report.pdf"
    get_info.assert_awaited_once_with(
        session,
        "yuanbao.tencent.com",
        {},
        expected_name,
    )
    upload.assert_awaited_once_with(
        session,
        config,
        b"pdf-data",
        "application/pdf",
    )
    assert result.filename == expected_name
    assert result.size == 8
    assert result.mime_type == "application/pdf"
    assert local_file.read_bytes() == b"pdf-data"


@pytest.fixture
def sample_upload_result():
    """Create a sample UploadResult."""
    from qwenpaw_channel_yuanbao.media import UploadResult

    return UploadResult(
        url="https://cdn.example.com/image.jpg",
        filename="image.jpg",
        size=12345,
        mime_type="image/jpeg",
        uuid_hex="abc123def456",
        width=800,
        height=600,
    )


class TestMediaHelpers:
    """P1: media.py helper function tests."""

    def test_guess_mime_jpeg(self):
        from qwenpaw_channel_yuanbao.media import _guess_mime

        assert _guess_mime("photo.jpg") == "image/jpeg"
        assert _guess_mime("photo.jpeg") == "image/jpeg"

    def test_guess_mime_png(self):
        from qwenpaw_channel_yuanbao.media import _guess_mime

        assert _guess_mime("icon.png") == "image/png"

    def test_guess_mime_pdf(self):
        from qwenpaw_channel_yuanbao.media import _guess_mime

        assert _guess_mime("doc.pdf") == "application/pdf"

    def test_guess_mime_unknown(self):
        from qwenpaw_channel_yuanbao.media import _guess_mime

        assert _guess_mime("data.xyz") == "application/octet-stream"

    def test_parse_image_size_png(self):
        from qwenpaw_channel_yuanbao.media import _parse_image_size

        # Minimal PNG header: width=100, height=200
        header = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
        header += struct.pack(">II", 100, 200)
        width, height = _parse_image_size(header)
        assert width == 100
        assert height == 200

    def test_parse_image_size_unknown(self):
        from qwenpaw_channel_yuanbao.media import _parse_image_size

        width, height = _parse_image_size(b"not an image")
        assert width == 0
        assert height == 0

    def test_resolve_local_path_file_uri(self, tmp_path):
        from qwenpaw_channel_yuanbao.media import _resolve_local_path

        local_file = tmp_path / "test image.jpg"
        assert _resolve_local_path(local_file.as_uri()) == str(local_file)

    def test_resolve_local_path_absolute(self, tmp_path):
        from qwenpaw_channel_yuanbao.media import _resolve_local_path

        local_file = tmp_path / "test.jpg"
        assert _resolve_local_path(str(local_file)) == str(local_file)

    def test_resolve_local_path_url_returns_none(self):
        from qwenpaw_channel_yuanbao.media import _resolve_local_path

        assert _resolve_local_path("https://example.com/img.jpg") is None

    def test_resolve_local_path_empty(self):
        from qwenpaw_channel_yuanbao.media import _resolve_local_path

        assert _resolve_local_path("") is None


class TestMediaMsgBody:
    """P1: TIMImageElem / TIMFileElem message body building."""

    def test_build_image_msg_body(self, sample_upload_result):
        from qwenpaw_channel_yuanbao.media import build_image_msg_body

        body = build_image_msg_body(sample_upload_result)

        assert len(body) == 1
        assert body[0]["msg_type"] == "TIMImageElem"
        content = body[0]["msg_content"]
        assert content["uuid"] == "abc123def456"
        assert content["image_format"] == 255
        images = content["image_info_array"]
        assert len(images) == 1
        assert images[0]["url"] == "https://cdn.example.com/image.jpg"
        assert images[0]["width"] == 800
        assert images[0]["height"] == 600
        assert images[0]["size"] == 12345

    def test_build_file_msg_body(self):
        from qwenpaw_channel_yuanbao.media import (
            UploadResult,
            build_file_msg_body,
        )

        result = UploadResult(
            url="https://cdn.example.com/doc.pdf",
            filename="report.pdf",
            size=99999,
            mime_type="application/pdf",
            uuid_hex="file_uuid_hex",
        )
        body = build_file_msg_body(result)

        assert len(body) == 1
        assert body[0]["msg_type"] == "TIMFileElem"
        content = body[0]["msg_content"]
        assert content["file_name"] == "report.pdf"
        assert content["file_size"] == 99999
        assert content["url"] == "https://cdn.example.com/doc.pdf"
        assert content["uuid"] == "file_uuid_hex"


class TestCosSignature:
    """P1: COS HMAC-SHA1 signature generation."""

    def test_sign_cos_request_format(self):
        from qwenpaw_channel_yuanbao.media import _sign_cos_request

        auth = _sign_cos_request(
            secret_id="AKIDxxx",
            secret_key="secretyyy",
            method="PUT",
            pathname="/upload/file.jpg",
            headers={"host": "bucket.cos.region.myqcloud.com"},
            start_time=1700000000,
            expired_time=1700003600,
        )

        assert "q-sign-algorithm=sha1" in auth
        assert "q-ak=AKIDxxx" in auth
        assert "q-sign-time=1700000000;1700003600" in auth
        assert "q-key-time=1700000000;1700003600" in auth
        assert "q-header-list=host" in auth
        assert "q-url-param-list=" in auth
        assert "q-signature=" in auth

    def test_sign_cos_request_deterministic(self):
        from qwenpaw_channel_yuanbao.media import _sign_cos_request

        args = {
            "secret_id": "id",
            "secret_key": "key",
            "method": "PUT",
            "pathname": "/path",
            "headers": {"host": "h"},
            "start_time": 100,
            "expired_time": 200,
        }
        assert _sign_cos_request(**args) == _sign_cos_request(**args)


class TestAuthSignature:
    """P1: Auth signature computation."""

    def test_compute_signature(self):
        from qwenpaw_channel_yuanbao.auth import _compute_signature

        sig = _compute_signature(
            nonce="abc123",
            timestamp="2026-01-01 00:00:00",
            app_id="test_key",
            app_secret="test_secret",
        )
        assert isinstance(sig, str)
        assert len(sig) > 0

    def test_compute_signature_deterministic(self):
        from qwenpaw_channel_yuanbao.auth import _compute_signature

        args = ("nonce1", "2026-01-01 00:00:00", "key", "secret")
        assert _compute_signature(*args) == _compute_signature(*args)

    def test_compute_signature_changes_with_nonce(self):
        from qwenpaw_channel_yuanbao.auth import _compute_signature

        sig1 = _compute_signature("nonce1", "ts", "key", "secret")
        sig2 = _compute_signature("nonce2", "ts", "key", "secret")
        assert sig1 != sig2


@pytest.mark.asyncio
class TestTokenManager:
    """P1: TokenManager lifecycle tests."""

    async def test_token_manager_init(self):
        from qwenpaw_channel_yuanbao.auth import TokenManager

        manager = TokenManager(
            app_id="test_key",
            app_secret="test_secret",
            api_domain="yuanbao.tencent.com",
        )

        assert manager.app_id == "test_key"
        assert manager.app_secret == "test_secret"
        assert manager._cache is None

    async def test_get_auth_headers(self):
        from qwenpaw_channel_yuanbao.auth import (
            SignTokenResult,
            TokenCache,
            TokenManager,
        )
        import time

        manager = TokenManager("k", "s")
        manager._cache = TokenCache(
            data=SignTokenResult(
                bot_id="bot_123",
                token="tok_abc",
                source="bot",
                duration=3600,
            ),
            expires_at=time.time() + 3600,
        )

        headers = await manager.get_auth_headers()
        assert headers["X-ID"] == "bot_123"
        assert headers["X-Token"] == "tok_abc"
        assert headers["X-Source"] == "bot"

        await manager.close()


class TestCodecHelpers:
    """P1: Codec encode/decode helpers."""

    @pytest.mark.parametrize(
        ("type_name", "payload", "expected"),
        [
            (
                "AUTH_BIND_RSP",
                {"code": 41101, "message": "already", "connectId": "cid"},
                {"code": 41101, "message": "already", "connectId": "cid"},
            ),
            (
                "PING_RSP",
                {"heartInterval": 42},
                {"heartInterval": 42},
            ),
            (
                "KICKOUT_MSG",
                {"status": 1, "reason": "another device"},
                {"status": 1, "reason": "another device"},
            ),
            (
                "AUTH_BIND_RSP",
                {"code": 0},
                {},
            ),
        ],
    )
    def test_pb_roundtrip(self, type_name, payload, expected):
        from qwenpaw_channel_yuanbao import codec

        message_type = getattr(codec, type_name)
        encoded = codec.encode_pb(message_type, payload)

        assert encoded is not None
        assert codec.decode_pb(message_type, encoded) == expected

    def test_to_proto_msg_body_text(self):
        from qwenpaw_channel_yuanbao.codec import _to_proto_msg_body

        elements = [
            {"msg_type": "TIMTextElem", "msg_content": {"text": "hello"}},
        ]
        result = _to_proto_msg_body(elements)
        assert len(result) == 1
        assert result[0]["msgType"] == "TIMTextElem"

    def test_to_proto_msg_body_image(self):
        from qwenpaw_channel_yuanbao.codec import _to_proto_msg_body

        elements = [
            {
                "msg_type": "TIMImageElem",
                "msg_content": {
                    "uuid": "img_uuid",
                    "image_format": 255,
                    "image_info_array": [
                        {
                            "type": 1,
                            "size": 1000,
                            "width": 100,
                            "height": 100,
                            "url": "https://cdn.example.com/img.jpg",
                        },
                    ],
                },
            },
        ]
        result = _to_proto_msg_body(elements)
        assert len(result) == 1
        assert result[0]["msgType"] == "TIMImageElem"
        # msgContent is a dict (not JSON string) in _to_proto_msg_body
        content = result[0]["msgContent"]
        assert content["uuid"] == "img_uuid"
        assert "imageInfoArray" in content

    def test_from_proto_msg_body(self):
        from qwenpaw_channel_yuanbao.codec import _from_proto_msg_body

        # _from_proto_msg_body expects msgContent as a dict, not JSON string
        elements = [
            {
                "msgType": "TIMTextElem",
                "msgContent": {"text": "world"},
            },
        ]
        result = _from_proto_msg_body(elements)
        assert len(result) == 1
        assert result[0]["msg_type"] == "TIMTextElem"
        assert result[0]["msg_content"]["text"] == "world"


class TestHelperFunctions:
    """P1: Helper function tests (_short_id, _sender_display)."""

    def test_short_id_long_string(self):
        from qwenpaw_channel_yuanbao.platform import _short_id

        result = _short_id(
            "CK8kfT4SpnXTsZg7ovLVTzWJLv8EymvNXO1BhLuAgOYwVFC1HLHzx5qq7AG0zjPq",
        )
        assert result == "7AG0zjPq"
        assert len(result) == 8

    def test_short_id_short_string(self):
        from qwenpaw_channel_yuanbao.platform import _short_id

        result = _short_id("abc")
        assert result == "abc"

    def test_sender_display_normal(self):
        from qwenpaw_channel_yuanbao.platform import _sender_display

        result = _sender_display(
            "灰",
            "CK8kfT4SpnXTsZg7ovLVTzWJLv8EymvNXO1BhLuAgOYwVFC1HLHzx5qq7AG0zjPq",
        )
        assert result == "灰#zjPq"

    def test_sender_display_empty_nickname(self):
        from qwenpaw_channel_yuanbao.platform import _sender_display

        result = _sender_display("", "abcdefgh")
        assert result == "unknown#efgh"

    def test_sender_display_short_id(self):
        from qwenpaw_channel_yuanbao.platform import _sender_display

        result = _sender_display("Test", "ab")
        assert result == "Test#ab"


class TestNormalizeInbound:
    """P1: _normalize_inbound tests."""

    def test_normalize_text_message(self):
        from qwenpaw_channel_yuanbao.platform import YuanbaoPlatform

        data = {
            "callback_command": "C2C.CallbackAfterSendMsg",
            "from_account": "sender123",
            "msg_body": [
                {"msg_type": "TIMTextElem", "msg_content": {"text": "你好"}},
            ],
        }
        result = YuanbaoPlatform._normalize_inbound(data)
        assert result["msg_body"][0]["msg_content"]["text"] == "你好"

    def test_normalize_string_content(self):
        from qwenpaw_channel_yuanbao.platform import YuanbaoPlatform

        data = {
            "msg_body": [
                {"msg_type": "TIMTextElem", "msg_content": '{"text": "hi"}'},
            ],
        }
        result = YuanbaoPlatform._normalize_inbound(data)
        assert result["msg_body"][0]["msg_content"] == {"text": "hi"}

    def test_normalize_invalid_json_string_content(self):
        from qwenpaw_channel_yuanbao.platform import YuanbaoPlatform

        data = {
            "msg_body": [
                {"msg_type": "TIMTextElem", "msg_content": "plain text"},
            ],
        }
        result = YuanbaoPlatform._normalize_inbound(data)
        assert result["msg_body"][0]["msg_content"] == {"text": "plain text"}

    def test_parses_cloud_custom_data_quote(self):
        from qwenpaw_channel_yuanbao.platform import YuanbaoPlatform

        raw = {
            "msg_body": [],
            "cloud_custom_data": (
                '{"quote":{"id":"abc","seq":1,"type":1,"desc":"hi"}}'
            ),
        }
        out = YuanbaoPlatform._normalize_inbound(raw)
        ccd = out["cloud_custom_data"]
        assert isinstance(ccd, dict)
        quote = ccd.get("quote")
        assert isinstance(quote, dict)
        assert quote.get("desc") == "hi"

    def test_cloud_custom_data_empty_string_becomes_empty_dict(self):
        from qwenpaw_channel_yuanbao.platform import YuanbaoPlatform

        out = YuanbaoPlatform._normalize_inbound(
            {"msg_body": [], "cloud_custom_data": ""},
        )
        assert not out["cloud_custom_data"]

    def test_cloud_custom_data_invalid_json_becomes_empty_dict(self):
        from qwenpaw_channel_yuanbao.platform import YuanbaoPlatform

        out = YuanbaoPlatform._normalize_inbound(
            {"msg_body": [], "cloud_custom_data": "{not json"},
        )
        assert not out["cloud_custom_data"]

    def test_cloud_custom_data_missing_field_becomes_empty_dict(self):
        from qwenpaw_channel_yuanbao.platform import YuanbaoPlatform

        out = YuanbaoPlatform._normalize_inbound({"msg_body": []})
        assert out["cloud_custom_data"] == {}


class TestResolveExtension:
    """``_resolve_extension`` should prefer the original filename suffix."""

    def test_filename_suffix_takes_priority_over_octet_stream(self):
        from qwenpaw_channel_yuanbao.utils import _resolve_extension

        # CDN often returns application/octet-stream; the real .txt must
        # not be silently rewritten to .bin.
        assert (
            _resolve_extension(
                "application/octet-stream",
                "陈灵威.txt",
            )
            == ".txt"
        )

    def test_filename_suffix_takes_priority_over_audio_mpeg(self):
        from qwenpaw_channel_yuanbao.utils import _resolve_extension

        # audio/mpeg may guess to .mpga depending on mime db; .mp3 must win.
        assert _resolve_extension("audio/mpeg", "test_副本.mp3") == ".mp3"

    def test_falls_back_to_content_type_when_no_filename_suffix(self):
        from qwenpaw_channel_yuanbao.utils import _resolve_extension

        ext = _resolve_extension("image/jpeg", "noext")
        assert ext in (".jpg", ".jpeg", ".jpe")

    def test_falls_back_to_bin_when_nothing_known(self):
        from qwenpaw_channel_yuanbao.utils import _resolve_extension

        assert _resolve_extension("", "") == ".bin"


class TestBuildSafeFilename:
    """``_build_safe_filename`` should preserve the original name."""

    def test_preserves_cjk_when_no_conflict(self, tmp_path):
        from qwenpaw_channel_yuanbao.utils import _build_safe_filename

        assert _build_safe_filename("陈灵威.txt", ".txt", tmp_path) == "陈灵威.txt"

    def test_strips_whitespace_keeps_parens(self, tmp_path):
        from qwenpaw_channel_yuanbao.utils import _build_safe_filename

        assert (
            _build_safe_filename("test 副本(1).mp3", ".mp3", tmp_path)
            == "test副本(1).mp3"
        )

    def test_appends_uid_only_on_conflict(self, tmp_path):
        from qwenpaw_channel_yuanbao.utils import _build_safe_filename

        existing = tmp_path / "陈灵威.txt"
        existing.write_text("seed")
        result = _build_safe_filename("陈灵威.txt", ".txt", tmp_path)
        assert result.startswith("陈灵威_")
        assert result.endswith(".txt")
        assert result != "陈灵威.txt"

    def test_blank_filename_uses_yuanbao_prefix(self, tmp_path):
        from qwenpaw_channel_yuanbao.utils import _build_safe_filename

        result = _build_safe_filename("", ".bin", tmp_path)
        assert result.startswith("yuanbao_")
        assert result.endswith(".bin")

    def test_all_whitespace_falls_back_to_file(self, tmp_path):
        from qwenpaw_channel_yuanbao.utils import _build_safe_filename

        # "  " (only whitespace) -> stripped to empty -> "file"
        assert _build_safe_filename("   .txt", ".txt", tmp_path) == "file.txt"


class TestClassifyFile:
    """``_classify_file`` routes audio extensions to AudioContent."""

    def test_audio_extensions(self):
        from qwenpaw_channel_yuanbao.platform import YuanbaoPlatform

        for name in (
            "test_副本.mp3",
            "voice.WAV",
            "a.m4a",
            "x.opus",
            "y.silk",
        ):
            assert YuanbaoPlatform._classify_file(name) == "audio"

    def test_non_audio_extensions(self):
        from qwenpaw_channel_yuanbao.platform import YuanbaoPlatform

        for name in ("陈灵威.txt", "doc.pdf", "img.png", "noext"):
            assert YuanbaoPlatform._classify_file(name) == "file"


class TestBuildQuotedPrefix:
    """``_build_quoted_prefix`` covers the 5 yuanbao quote scenarios."""

    def test_text_quote(self):
        from qwenpaw_channel_yuanbao.platform import YuanbaoPlatform

        assert (
            YuanbaoPlatform._build_quoted_prefix(
                {"type": 1, "desc": "你好呀"},
            )
            == "[quoted message: 你好呀]"
        )

    def test_image_quote_empty_desc(self):
        from qwenpaw_channel_yuanbao.platform import YuanbaoPlatform

        assert (
            YuanbaoPlatform._build_quoted_prefix({"type": 2, "desc": ""})
            == "[quoted image]"
        )

    def test_file_quote_with_filename(self):
        from qwenpaw_channel_yuanbao.platform import YuanbaoPlatform

        assert (
            YuanbaoPlatform._build_quoted_prefix(
                {"type": 3, "desc": "陈灵威.txt"},
            )
            == "[quoted file: 陈灵威.txt]"
        )

    def test_audio_quote_routed_by_suffix(self):
        from qwenpaw_channel_yuanbao.platform import YuanbaoPlatform

        assert (
            YuanbaoPlatform._build_quoted_prefix(
                {"type": 3, "desc": "test_副本.mp3"},
            )
            == "[quoted audio: test_副本.mp3]"
        )

    def test_file_type_with_empty_desc(self):
        from qwenpaw_channel_yuanbao.platform import YuanbaoPlatform

        assert (
            YuanbaoPlatform._build_quoted_prefix({"type": 3, "desc": ""})
            == "[quoted file]"
        )

    def test_unknown_type_falls_back_to_message(self):
        from qwenpaw_channel_yuanbao.platform import YuanbaoPlatform

        assert (
            YuanbaoPlatform._build_quoted_prefix({"type": 99, "desc": ""})
            == "[quoted message]"
        )

    def test_non_dict_returns_none(self):
        from qwenpaw_channel_yuanbao.platform import YuanbaoPlatform

        assert YuanbaoPlatform._build_quoted_prefix(None) is None
        assert YuanbaoPlatform._build_quoted_prefix("oops") is None



@pytest.mark.integration
@pytest.mark.p1
def test_yuanbao_short_id() -> None:
    """Short id keeps the trailing suffix."""
    from qwenpaw_channel_yuanbao.platform import _short_id

    long_id = "0123456789abcdef"
    short = _short_id(long_id)
    assert long_id.endswith(short)
    assert len(short) <= len(long_id)
    assert _short_id("ab") == "ab"


@pytest.mark.integration
@pytest.mark.p1
def test_yuanbao_sender_display() -> None:
    """Sender display combines nickname and trailing id digits."""
    from qwenpaw_channel_yuanbao.platform import _sender_display

    display = _sender_display("Alice", "1234567890")
    assert "Alice" in display
    assert display.endswith("7890")
