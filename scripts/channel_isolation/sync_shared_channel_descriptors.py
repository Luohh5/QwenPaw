#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate or check candidate static configuration schemas at build time."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def main() -> int:
    """Keep inherited configuration defaults in the static descriptors."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / "src"))
    # This development-only command is the sole schema-generation importer.
    from qwenpaw.config.config import DingTalkConfig, FeishuConfig
    from qwenpaw.channel_protocol.shared import SharedChannelDescriptor

    stale = []
    for key, model, label, driver, capabilities in (
        (
            "feishu",
            FeishuConfig,
            {"en": "Feishu / Lark", "zh": "飞书"},
            "FeishuDriver",
            ["approval_card", "media", "reaction", "streaming"],
        ),
        (
            "dingtalk",
            DingTalkConfig,
            {"en": "DingTalk", "zh": "钉钉"},
            "DingTalkDriver",
            ["approval_card", "media", "streaming"],
        ),
    ):
        schema = model.model_json_schema()
        for name, default in model().model_dump(mode="json").items():
            schema["properties"][name]["default"] = default
        descriptor = SharedChannelDescriptor.from_mapping(
            {
                "schema_version": 2,
                "channel_key": key,
                "label": label,
                "process_mode": "runner_process",
                "dependency_mode": "shared",
                "entrypoint": {
                    "scope": "runner",
                    "module": key + ".driver",
                    "qualname": driver,
                },
                "requirements_file": "requirements.txt",
                "config_schema": schema,
                "config_fields": list(schema["properties"]),
                "capabilities": capabilities,
            },
        )
        path = root / "plugins" / "channel" / key / "channel.json"
        content = (
            json.dumps(descriptor.to_mapping(), ensure_ascii=False, indent=2)
            + "\n"
        )
        if args.check:
            if (
                not path.exists()
                or path.read_text(encoding="utf-8") != content
            ):
                stale.append(str(path.relative_to(root)))
        else:
            path.write_text(content, encoding="utf-8")
    if stale:
        print("Stale shared descriptors: " + ", ".join(stale), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
