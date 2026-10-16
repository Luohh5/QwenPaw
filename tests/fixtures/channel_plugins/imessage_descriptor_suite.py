"""Consume the real private descriptor through the production public reader."""

from pathlib import Path

from qwenpaw.channel_protocol.shared import SharedChannelDescriptor


def test_imessage_descriptor_is_consumable_by_public_reader():
    path = (
        Path(__file__).resolve().parents[3]
        / "plugins/channel/imessage/channel.json"
    )
    descriptor = SharedChannelDescriptor.load(path)
    assert descriptor.channel_key == "imessage"
    assert descriptor.config_schema["properties"]["poll_sec"]["default"] == 1.0
    assert set(descriptor.config_fields) == set(
        descriptor.config_schema["properties"]
    )
