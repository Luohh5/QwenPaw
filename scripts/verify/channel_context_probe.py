#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Disposable Actions acceptance for channel worker runtime directories."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile


async def worker(args):
    """Launch a real worker with packaged WeChat imports, without network."""
    root = args.data_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    os.environ["QWENPAW_WORKING_DIR"] = str(root)
    os.environ["QWENPAW_DEPENDENCY_STATE_DIR"] = str(root / "dependency-state")
    os.environ.pop("CHANNEL_ACTIONS_ENV_PROBE", None)
    if args.plain:
        os.environ.pop("QWENPAW_RUNTIME_ID", None)
    else:
        os.environ["QWENPAW_RUNTIME_ID"] = "channel-actions-runtime"
    if args.missing_home:
        os.environ.pop("HOME", None)
    if args.assert_no_passwd:
        import pwd

        try:
            pwd.getpwuid(os.getuid())
        except KeyError:
            pass
        else:
            raise AssertionError(
                "Expected the real sandbox UID lookup to fail",
            )
    (root / ".env").write_text(
        "CHANNEL_ACTIONS_ENV_PROBE=loaded\n",
        encoding="utf-8",
    )

    import qwenpaw
    from qwenpaw.__version__ import __version__
    from qwenpaw.constant import WORKING_DIR
    from qwenpaw.app.channels.process_supervisor import ChannelProcess
    from qwenpaw.app.channels.process_supervisor import LaunchSpec
    from qwenpaw.channel_protocol.artifacts import code_root_digest
    from qwenpaw.channel_protocol.shared import (
        SharedChannelDescriptor,
        _read_requirements,
    )
    from qwenpaw.plugins.execution import PluginProcess, ProcessError

    package = Path(qwenpaw.__file__).resolve().parent
    if args.installed:
        assert package.is_relative_to(Path(sys.prefix).resolve()), package
    index = json.loads(
        (package / "official_channels/index.json").read_text("utf-8"),
    )
    artifact = next(
        row for row in index["plugins"] if row["plugin_id"] == "wechat"
    )
    code = root / "plugin"
    code.mkdir()
    with zipfile.ZipFile(
        package / "official_channels" / artifact["filename"],
    ) as archive:
        archive.extractall(code)
    descriptor_path = next(code.rglob("channel.json"))
    code = descriptor_path.parent
    descriptor_data = json.loads(descriptor_path.read_text("utf-8"))
    descriptor_data["entrypoint"] = {
        "scope": "runner",
        "module": "context_probe",
        "qualname": "Driver",
    }
    descriptor_path.write_text(json.dumps(descriptor_data), encoding="utf-8")
    expected_id = None if args.plain else "channel-actions-runtime"
    expected_env = "loaded" if args.plain else None
    expected_home = (
        str(Path(os.environ["HOME"]).absolute())
        if os.name != "nt" and os.environ.get("HOME")
        else None
    )
    missing_user = (
        "if os.name != 'nt':\n"
        "    import pwd\n"
        "    def missing(uid): raise KeyError(uid)\n"
        "    pwd.getpwuid = missing\n"
        if not args.assert_no_passwd
        else ""
    )
    (code / "context_probe.py").write_text(
        "import os\n"
        + missing_user
        + "from qwenpaw_channel_wechat.platform import WeChatPlatform\n"
        "from qwenpaw.constant import WORKING_DIR\n"
        f"assert str(WORKING_DIR) == {str(WORKING_DIR)!r}\n"
        f"assert os.environ['QWENPAW_WORKING_DIR'] == {str(WORKING_DIR)!r}\n"
        f"assert os.environ.get('QWENPAW_RUNTIME_ID') == {expected_id!r}\n"
        f"assert os.environ.get('CHANNEL_ACTIONS_ENV_PROBE') == {expected_env!r}\n"
        f"assert os.environ.get('HOME') == {expected_home!r}\n"
        "class Driver:\n"
        "    def bind(self, context): self.context = context\n"
        "    async def start(self, config):\n"
        "        await self.context.set_platform_state('connected')\n"
        "    async def stop(self): pass\n"
        "    @staticmethod\n"
        "    async def manage(request): pass\n",
        encoding="utf-8",
    )
    # The parent has already resolved its directory; the child must not
    # reinterpret this raw relative setting from the plugin's own cwd.
    os.environ["QWENPAW_WORKING_DIR"] = "./unresolved-data"
    if args.negative:
        ChannelProcess._launch_environment = PluginProcess._launch_environment
    descriptor = SharedChannelDescriptor.load(descriptor_path)
    spec = LaunchSpec(
        python_executable=sys.executable,
        support_root=package.parent,
        code_root=code,
        descriptor_path=descriptor_path,
        source_revision=code_root_digest(code),
        descriptor=descriptor,
        requirements=_read_requirements(code / descriptor.requirements_file),
        channel_key="wechat",
        instance_id="actions-runtime-context",
        plugin_id="wechat",
        workspace_id="actions-workspace",
        generation=1,
        qwenpaw_version=__version__,
        config={},
        startup_timeout=20,
    )
    process = ChannelProcess()
    method = {
        "run": process.start,
        "manage": process.start_management,
        "probe": process.probe,
    }[args.mode]
    rejected = False
    try:
        try:
            await method(spec)
            if args.mode == "run":
                async with asyncio.timeout(20):
                    while (
                        process.health_snapshot()["platform_state"]
                        != "connected"
                    ):
                        if (
                            process.health_snapshot()["platform_state"]
                            == "failed"
                        ):
                            raise AssertionError("Worker component failed")
                        await asyncio.sleep(0.02)
        except ProcessError:
            if not args.negative:
                raise
            rejected = True
        assert rejected == args.negative
    finally:
        stopped = await process.stop()
        assert stopped.exited and not stopped.residual
    print(
        json.dumps(
            {
                "mode": args.mode,
                "package": str(package),
                "working_dir": str(WORKING_DIR),
                "missing_home": args.missing_home,
                "real_missing_passwd": args.assert_no_passwd,
                "negative_rejected": rejected,
                "result": "passed",
            },
            ensure_ascii=True,
        ),
    )


def bubblewrap(args):
    """Use the project's actual Hub isolator with a genuinely absent passwd."""
    from qwenpaw.hub.local_provisioner import LocalProcessRuntimeProvisioner
    from qwenpaw.hub.models import RuntimeRecord, RuntimeState
    from qwenpaw.hub.process_isolation import LinuxBubblewrapIsolator

    root = args.data_root.resolve() / "runtime"
    for name in ("working", "secrets", "backups", "logs", "working/tmp"):
        (root / name).mkdir(parents=True, exist_ok=True)
    record = RuntimeRecord(
        runtime_id="actions-runtime",
        tenant_id="actions-tenant",
        owner_user_id="actions-user",
        provisioner="local",
        host="127.0.0.1",
        port=9876,
        state=RuntimeState.CREATED,
        working_dir=root / "working",
        secret_dir=root / "secrets",
        backup_dir=root / "backups",
        log_file=root / "logs/runtime.log",
    )
    script = record.working_dir / "context_probe.py"
    script.write_bytes(Path(__file__).read_bytes())
    environment = LocalProcessRuntimeProvisioner.runtime_environment(
        record,
        {},
    )
    environment["QWENPAW_DEPENDENCY_STATE_DIR"] = "/workspace/dependency-state"
    for negative in (True, False):
        command = [
            sys.executable,
            "-I",
            "/workspace/context_probe.py",
            "worker",
            "--data-root",
            "/workspace/negative" if negative else "/workspace/数据 data",
            "--missing-home",
            "--assert-no-passwd",
        ]
        if negative:
            command.append("--negative")
        launch = LinuxBubblewrapIsolator().prepare(
            record,
            command,
            environment,
        )
        subprocess.run(
            launch.command,
            env=launch.environment,
            cwd=root,
            check=True,
            timeout=90,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("worker", "bubblewrap"))
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument(
        "--mode",
        choices=("run", "manage", "probe"),
        default="run",
    )
    parser.add_argument("--plain", action="store_true")
    parser.add_argument("--missing-home", action="store_true")
    parser.add_argument("--assert-no-passwd", action="store_true")
    parser.add_argument("--installed", action="store_true")
    parser.add_argument("--negative", action="store_true")
    args = parser.parse_args()
    if args.kind == "bubblewrap":
        bubblewrap(args)
    else:
        asyncio.run(worker(args))


if __name__ == "__main__":
    main()
