"""Real private Azure attachment boundary security regressions."""
# flake8: noqa: F811

from pathlib import Path

import pytest

from azure_bot_native_support import (  # noqa: F401
    azure,
    connector,
    rsa_material,
)


@pytest.mark.asyncio
async def test_unsafe_attachment_never_falls_back_to_untrusted_url(azure):
    async with azure() as (driver, _):
        part = await driver._platform._attachment(
            {
                "contentUrl": "http://media.test/download/image",
                "contentType": "image/png",
            },
            "sensitive-bot-token",
        )
    assert part is None


@pytest.mark.asyncio
async def test_external_attachment_does_not_receive_bot_token(
    azure, connector
):
    async with azure() as (driver, _):
        part = await driver._platform._attachment(
            {
                "contentUrl": "https://media.test/download/image",
                "contentType": "image/png",
            },
            "sensitive-bot-token",
        )
        assert part and Path(part["image_url"]).read_bytes().startswith(
            b"\x89PNG"
        )
    assert connector.requests == [("download", "/download/image", None)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://media.test/download/image",
        "https://user:pass@media.test/download/image",
        "https://@media.test/download/image",
        "https://media.test:444/download/image",
        "https://127.0.0.1/download/image",
        "https://10.0.0.1/download/image",
        "https://169.254.169.254/latest/meta-data",
        "https://[::1]/download/image",
        "https://[fec0::1]/download/image",
        "https://[fecf:abcd::1]/download/image",
        "https://[::ffff:127.0.0.1]/download/image",
        "https://224.0.0.1/download/image",
        "https://[64:ff9b::7f00:1]/download/image",
        "https://[2002:7f00:1::]/download/image",
        "https://[2001:0:4136:e378:8000:63bf:3fff:fdd2]/download/image",
        "file:///etc/passwd",
    ],
)
async def test_unsafe_url_is_rejected_before_dns_or_socket(
    azure, connector, url
):
    async with azure() as (driver, _):
        result = await driver._platform._attachment(
            {"contentUrl": url, "contentType": "image/png"}, "secret"
        )
        assert result is None
        assert connector.dns_calls == connector.connections == []
        assert connector.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "addresses",
    [
        ["127.0.0.1"],
        ["10.3.2.1"],
        ["169.254.169.254"],
        ["::1"],
        ["fc00::1"],
        ["fec0::1"],
        ["fecf:abcd::1"],
        ["::ffff:127.0.0.1"],
        ["93.184.216.34", "127.0.0.1"],
        ["127.0.0.1", "93.184.216.34"],
        ["93.184.216.34", "fc00::1"],
        ["224.0.0.1"],
    ],
)
async def test_private_or_mixed_dns_cannot_download(
    azure, connector, addresses
):
    connector.dns_answers["media.test"] = addresses
    async with azure() as (driver, _):
        result = await driver._platform._attachment(
            {
                "contentUrl": "https://media.test/download/image",
                "contentType": "image/png",
            },
            "secret",
        )
        assert result is None
        assert connector.dns_calls == ["media.test"]
        assert connector.connections == connector.requests == []


@pytest.mark.asyncio
async def test_rebinding_cannot_replace_validated_socket_ip(azure, connector):
    answers = iter([["93.184.216.34"], ["127.0.0.1"]])
    connector.dns_answers["media.test"] = lambda: next(answers)
    async with azure() as (driver, _):
        result = await driver._platform._attachment(
            {
                "contentUrl": "https://media.test/download/image",
                "contentType": "image/png",
            },
            "secret",
        )
        assert result and Path(result["image_url"]).read_bytes() == (
            b"\x89PNG\r\n\x1a\nimage"
        )
        assert connector.dns_calls == ["media.test"]
        assert connector.connections == [("93.184.216.34", 443)]
        assert connector.requests == [("download", "/download/image", None)]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["redirect", "missing"])
async def test_redirect_and_failed_download_never_escape_as_raw_url(
    azure, connector, path
):
    async with azure() as (driver, _):
        result = await driver._platform._attachment(
            {
                "contentUrl": "https://media.test/download/" + path,
                "contentType": "image/png",
            },
            "secret",
        )
        assert result is None
    assert connector.requests == [("download", "/download/" + path, None)]


@pytest.mark.asyncio
async def test_signed_connector_origin_retains_authenticated_download(
    azure, connector
):
    from azure_bot_native_support import activity, post

    async with azure() as (driver, peer):
        status, _ = await post(
            driver,
            connector,
            activity(
                attachments=[
                    {
                        "contentUrl": "https://connector.test/download/image",
                        "contentType": "image/png",
                    }
                ]
            ),
        )
        assert status == 200
        image = peer.events[0].content_parts[1]
        assert (
            Path(image["image_url"]).read_bytes() == b"\x89PNG\r\n\x1a\nimage"
        )
    assert connector.requests == [
        ("download", "/download/image", "Bearer access-1")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mime, expected_type, field",
    [
        ("image/png", "image", "image_url"),
        ("video/mp4", "video", "video_url"),
        ("audio/ogg", "audio", "data"),
        ("application/pdf", "file", "file_url"),
    ],
)
async def test_safe_public_media_still_becomes_local_content(
    azure, connector, mime, expected_type, field
):
    async with azure() as (driver, _):
        part = await driver._platform._attachment(
            {
                "contentUrl": "https://media.test/download/image",
                "contentType": mime,
                "name": "../original.bin",
            },
            "secret",
        )
        assert part["type"] == expected_type
        path = Path(part[field])
        assert path.is_relative_to(driver._platform.media_dir)
        assert path.read_bytes() == b"\x89PNG\r\n\x1a\nimage"
        if expected_type == "audio":
            assert part["format"] == "ogg"
        if expected_type == "file":
            assert part["filename"] == "../original.bin"
    assert connector.requests == [("download", "/download/image", None)]


@pytest.mark.asyncio
async def test_inline_data_attachment_stays_nonnetwork(azure, connector):
    value = "data:image/png;base64,aGVsbG8="
    async with azure() as (driver, _):
        result = await driver._platform._attachment(
            {"contentUrl": value, "contentType": "image/png"}, "secret"
        )
        assert result == {"type": "image", "image_url": value}
        assert connector.dns_calls == connector.connections == []
        assert connector.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "host",
    ["api.botframework.com", "smba.trafficmanager.net", "botapi.skype.com"],
)
async def test_known_bot_framework_attachment_host_receives_token(
    azure, connector, host
):
    async with azure() as (driver, _):
        part = await driver._platform._attachment(
            {
                "contentUrl": "https://" + host + "/download/image",
                "contentType": "image/png",
            },
            "secret",
        )
        assert part and Path(part["image_url"]).is_file()
    assert connector.requests == [
        ("download", "/download/image", "Bearer secret")
    ]


@pytest.mark.asyncio
async def test_public_activity_media_survives_bot_credential_failure(
    azure, connector, monkeypatch
):
    from azure_bot_native_support import activity, platform_module, post

    def unavailable_credentials(*args, **kwargs):
        raise RuntimeError("external credential issuer unavailable")

    monkeypatch.setattr(
        platform_module.msal,
        "ConfidentialClientApplication",
        unavailable_credentials,
    )
    async with azure() as (driver, peer):
        status, _ = await post(
            driver,
            connector,
            activity(
                attachments=[
                    {
                        "contentUrl": "https://media.test/download/image",
                        "contentType": "image/png",
                    }
                ]
            ),
        )
        assert status == 200
        assert len(peer.events) == 1
        path = Path(peer.events[0].content_parts[1]["image_url"])
        assert path.read_bytes() == b"\x89PNG\r\n\x1a\nimage"
    assert connector.requests == [("download", "/download/image", None)]
    assert connector.token_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "host",
    ["api.botframework.com.attacker.test", "tenant.blob.core.windows.net"],
)
async def test_public_lookalike_or_azure_tenant_host_gets_no_bot_token(
    azure, connector, host
):
    async with azure() as (driver, _):
        result = await driver._platform._attachment(
            {
                "contentUrl": "https://" + host + "/download/image",
                "contentType": "image/png",
            },
            "secret",
        )
        assert result and Path(result["image_url"]).is_file()
    assert connector.requests == [("download", "/download/image", None)]


@pytest.mark.asyncio
async def test_tls_hostname_mismatch_cannot_be_downloaded(azure, connector):
    async with azure() as (driver, _):
        result = await driver._platform._attachment(
            {
                "contentUrl": "https://mismatch.test/download/image",
                "contentType": "image/png",
            },
            "secret",
        )
        assert result is None
        assert connector.connections == [("93.184.216.34", 443)]
        assert connector.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_stop", [False, True])
async def test_attachment_network_resources_close_after_owned_stop(
    azure, connector, monkeypatch, failed_stop
):
    async with azure() as (driver, _):
        platform = driver._platform
        await platform._attachment(
            {
                "contentUrl": "https://media.test/download/image",
                "contentType": "image/png",
            },
            "secret",
        )
        session = platform._attachment_session
        tcp_connector = session.connector
        dns = platform._attachment_resolver.resolver
        if failed_stop:
            with monkeypatch.context() as patch:

                async def fails(*args):
                    raise RuntimeError("fixture stop failure")

                patch.setattr(type(platform.site), "stop", fails)
                with pytest.raises(RuntimeError, match="fixture stop failure"):
                    await driver.stop()
                assert not session.closed
                assert not dns.closed
        await driver.stop()
        assert session.closed and tcp_connector.closed
        assert dns.closed
        assert platform._attachment_session is None
        assert platform._attachment_resolver is None


@pytest.mark.parametrize(
    "host", ["127.000.000.001", "010.0.0.1", "127.000.0.1", "000.0.0.0"]
)
def test_ambiguous_numeric_host_is_rejected_before_aiohttp_version_checks(
    host,
):
    # aiohttp 3.8 classifies padded IPv4 literals without consulting a resolver.
    from qwenpaw_channel_azure_bot.attachments import attachment_url

    with pytest.raises(ValueError):
        attachment_url("https://" + host + "/download/image")
