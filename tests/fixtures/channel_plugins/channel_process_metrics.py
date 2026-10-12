# -*- coding: utf-8 -*-
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=protected-access
# pylint: disable=import-outside-toplevel,too-many-locals
# pylint: disable=too-many-statements,too-few-public-methods
# pylint: disable=consider-using-with,using-constant-test
# pylint: disable=unused-import
"""Fresh-process B5 metrics; real SDKs, fake platform network, no live calls.

python -I tests/fixtures/channel_plugins/channel_process_metrics.py --repeats 3
Writes /tmp/qwenpaw-b5-measurements.json. RSS is current psutil RSS, not peak
or physical RAM. Controlled readiness includes external fixture setup/import.
"""

import argparse
import asyncio
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time

import psutil

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).parent
SDK = {
    "feishu": ("lark_oapi",),
    "dingtalk": ("dingtalk_stream", "alibabacloud_dingtalk"),
}


def sdk_count(channel):
    return sum(
        any(name == p or name.startswith(p + ".") for p in SDK[channel])
        for name in sys.modules
    )


def rss(pid=None):
    return psutil.Process(pid or os.getpid()).memory_info().rss


def origins(channel, code):
    files = {
        name: mod.__file__
        for name, mod in list(sys.modules.items())
        if name
        in [
            f"{channel}.driver",
            f"{channel}.platform",
            f"{channel}.transport",
            f"{channel}.channel",
        ]
    }
    assert files and all(
        Path(p).resolve().is_relative_to(code.resolve())
        for p in files.values()
    )
    return files


async def probe(channel, mode, experiment):
    sys.path.insert(0, str(ROOT / "src"))
    # Same host support warming in both candidates; timing below excludes this.
    import qwenpaw.app.channels.base  # noqa: F401
    from qwenpaw.config.config import FeishuConfig, DingTalkConfig
    from qwenpaw.app.channels.isolated_proxy import IsolatedChannelProxy

    class NoBuiltin:
        def find_spec(self, fullname, *_args):
            if fullname.startswith(
                (
                    "qwenpaw.app.channels.feishu",
                    "qwenpaw.app.channels.dingtalk",
                ),
            ):
                raise AssertionError("builtin fallback: " + fullname)

    sys.meta_path.insert(0, NoBuiltin())
    if experiment == "fixture-connected":
        # Equalize fixture framework memory in both measured host candidates.
        importlib.import_module("pytest")
        importlib.import_module("pytest_asyncio")
    with tempfile.TemporaryDirectory(prefix="qwenpaw-b5-metrics-") as temp:
        work = Path(temp) / "work"
        work.mkdir()
        code = Path(temp) / "installed"
        plugin = code / channel
        shutil.copytree(
            ROOT / "plugins/channel" / channel,
            plugin,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        started = time.perf_counter()
        initial_rss = rss()
        record = {
            "channel": channel,
            "mode": mode,
            "experiment": experiment,
            "pid": os.getpid(),
            "initial_parent_rss_bytes": initial_rss,
            "initial_parent_sdk_modules": sdk_count(channel),
        }
        if experiment == "cold-import-only":
            if mode == "same-process":
                sys.path.insert(0, str(code))
                importlib.import_module(f"{channel}.driver")
                record.update(
                    import_seconds=time.perf_counter() - started,
                    parent_rss_bytes=rss(),
                    child_rss_bytes=0,
                    parent_sdk_modules=sdk_count(channel),
                    origins=origins(channel, code),
                )
            else:
                child_code = """import importlib,json,os,sys,time,psutil
sys.path.insert(0,sys.argv[1]);sys.path.insert(0,sys.argv[2])
import qwenpaw.app.channels.base
from qwenpaw.config.config import FeishuConfig,DingTalkConfig
from qwenpaw.app.channels.isolated_proxy import IsolatedChannelProxy
t=time.perf_counter();importlib.import_module(sys.argv[3]+'.driver')
mods={n:m.__file__ for n,m in list(sys.modules.items()) if n in
      [sys.argv[3]+'.driver',sys.argv[3]+'.platform',sys.argv[3]+'.transport']}
print(json.dumps({'pid':os.getpid(),'import_seconds':time.perf_counter()-t,
'rss':psutil.Process().memory_info().rss,
'sdk_modules':sum(n.startswith(('lark_oapi','dingtalk_stream',
'alibabacloud_dingtalk')) for n in sys.modules),
'origins':mods}),flush=True)
input()
"""
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-I",
                        "-c",
                        child_code,
                        str(code),
                        str(ROOT / "src"),
                        channel,
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    text=True,
                )
                try:
                    sample = json.loads(child.stdout.readline())
                    assert all(
                        Path(p).resolve().is_relative_to(plugin.resolve())
                        for p in sample["origins"].values()
                    )
                    record.update(
                        import_seconds=sample["import_seconds"],
                        spawn_to_import_seconds=time.perf_counter() - started,
                        child_pid=sample["pid"],
                        parent_rss_bytes=rss(),
                        child_rss_bytes=rss(sample["pid"]),
                        parent_sdk_modules=sdk_count(channel),
                        child_sdk_modules=sample["sdk_modules"],
                        origins=sample["origins"],
                    )
                    stop = time.perf_counter()
                    child.communicate("\n", timeout=5)
                    record.update(
                        stop_seconds=time.perf_counter() - stop,
                        child_alive_after_stop=psutil.pid_exists(
                            sample["pid"],
                        ),
                    )
                finally:
                    if child.poll() is None:
                        child.kill()
                    child.wait(timeout=5)
                    child.stdin.close()
                    child.stdout.close()
        elif mode == "subprocess":
            sys.path.insert(0, str(HERE))
            contracts = importlib.import_module(f"{channel}_process_suite")
            construction = []

            def timed_proxy(*args, **kwargs):
                before = time.perf_counter()
                value = IsolatedChannelProxy(*args, **kwargs)
                construction.append(time.perf_counter() - before)
                return value

            gen = contracts.runtime.__wrapped__(timed_proxy, Path(temp))
            make = await anext(gen)
            stamps = []
            real_start = IsolatedChannelProxy.start

            async def measured_start(instance):
                stamps.append(time.perf_counter())
                return await real_start(instance)

            IsolatedChannelProxy.start = measured_start
            original_read = Path.read_text
            boundary_path = HERE / f"{channel}_process_boundary.py"

            def instrument_read(path, *a, **kw):
                text = original_read(path, *a, **kw)
                if path == boundary_path:
                    text += (
                        "\nimport sys\nrecord('measurement-imports', "
                        "sdk_modules=sum(any(n == p or "
                        "n.startswith(p + '.') for p in "
                        + repr(SDK[channel])
                        + ") for n in sys.modules), origins={n:m.__file__ "
                        "for n,m in list(sys.modules.items()) if n in "
                        + repr(
                            [
                                f"{channel}.driver",
                                f"{channel}.platform",
                                f"{channel}.transport",
                            ],
                        )
                        + "})\n"
                    )
                return text

            Path.read_text = instrument_read
            try:
                rt = await make()
            finally:
                Path.read_text = original_read
                IsolatedChannelProxy.start = real_start
            child_imports = rt.rows("measurement-imports")[0]
            assert all(
                Path(p)
                .resolve()
                .is_relative_to((rt.spec.code_root / channel).resolve())
                for p in child_imports["origins"].values()
            ), child_imports
            child_pid = rt.process.health_snapshot()["pid"]
            record.update(
                readiness_seconds=time.perf_counter() - stamps[0],
                preparation_seconds=stamps[0] - started,
                preparation_scope=(
                    "fixture import/copy, " "proxy construction before start"
                ),
                construction_seconds=construction[0],
                child_pid=child_pid,
                parent_rss_bytes=rss(),
                child_rss_bytes=rss(child_pid),
                parent_sdk_modules=sdk_count(channel),
                child_sdk_modules=child_imports["sdk_modules"],
                origins=child_imports["origins"],
                health_ready=rt.process.health_snapshot(),
            )
            stop = time.perf_counter()
            await rt.channel.stop()
            record.update(
                stop_seconds=time.perf_counter() - stop,
                health_stopped=rt.process.health_snapshot(),
                child_alive_after_stop=psutil.pid_exists(child_pid),
            )
            await gen.aclose()
        else:
            sys.path.insert(0, str(code))
            boundary = (HERE / f"{channel}_process_boundary.py").read_text()
            # Baseline must import the same-process candidate, not the Driver.
            boundary = boundary[: boundary.rfind("from .driver import")]
            # The B4 child fixture asserts host absence; baseline is the host.
            boundary = boundary.replace(
                'assert "qwenpaw.app.channels.base" not in sys.modules',
                "pass",
            )
            (plugin / "boundary.py").write_text(
                f"WORK={str(work)!r}\n" + boundary,
            )
            candidate = importlib.import_module(f"{channel}.channel")
            config = {"enabled": True, "media_dir": str(work / "media")}
            if channel == "feishu":
                cls, schema = candidate.FeishuChannel, FeishuConfig
                config.update(app_id="cli_test", app_secret="test-only")
            else:
                cls, schema = candidate.DingTalkChannel, DingTalkConfig
                config.update(
                    client_id="client-one",
                    client_secret="test-only",
                )

            async def agent(_request):
                if False:
                    yield None

            constructor_start = time.perf_counter()
            instance = cls.from_config(
                agent,
                schema(**config),
                workspace_dir=work,
            )
            record["construction_seconds"] = (
                time.perf_counter() - constructor_start
            )
            record["preparation_seconds"] = time.perf_counter() - started
            preparation = time.perf_counter()
            importlib.import_module(f"{channel}.boundary")
            await instance.start()
            async with asyncio.timeout(8):
                while (
                    not (work / "ledger.jsonl").exists()
                    or '"connected"' not in (work / "ledger.jsonl").read_text()
                ):
                    await asyncio.sleep(0.01)
            record.update(
                readiness_seconds=time.perf_counter() - preparation,
                preparation_scope=(
                    "package/Channel imports and "
                    "Channel construction before readiness"
                ),
                parent_rss_bytes=rss(),
                child_rss_bytes=0,
                parent_sdk_modules=sdk_count(channel),
                origins=origins(channel, code),
            )
            platform_owner = (
                instance._platform if channel == "feishu" else instance
            )
            thread = getattr(
                platform_owner,
                "_ws_thread" if channel == "feishu" else "_stream_thread",
            )
            stop = time.perf_counter()
            await instance.stop()
            record["stop_seconds"] = time.perf_counter() - stop
            record["same_process_stop"] = {
                "state": instance._state,
                "last_error": instance._last_error,
                "owned_thread_alive": thread.is_alive() if thread else False,
            }
        record["summed_current_rss_bytes"] = (
            record["parent_rss_bytes"] + record["child_rss_bytes"]
        )
        record["parent_rss_after_stop_bytes"] = rss()
        return record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", nargs=3)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.probe:
        print(json.dumps(asyncio.run(probe(*args.probe))))
        return
    samples = []
    for experiment in ("cold-import-only", "fixture-connected"):
        for channel in SDK:
            for mode in ("same-process", "subprocess"):
                for repeat in range(args.repeats):
                    result = subprocess.run(
                        [
                            sys.executable,
                            "-I",
                            str(Path(__file__).resolve()),
                            "--probe",
                            channel,
                            mode,
                            experiment,
                        ],
                        env={
                            **os.environ,
                            "QWENPAW_WORKING_DIR": (
                                "/tmp/qwenpaw-b5-metrics-host"
                            ),
                        },
                        capture_output=True,
                        text=True,
                        timeout=30,
                        check=True,
                    )
                    sample = json.loads(result.stdout.splitlines()[-1])
                    sample.update(repeat=repeat, stderr=result.stderr)
                    samples.append(sample)
    versions = {
        name: importlib.metadata.version(name)
        for name in (
            "lark-oapi",
            "dingtalk-stream",
            "alibabacloud-dingtalk",
            "psutil",
            "aiohttp",
        )
    }
    output = {
        "interpreter": sys.executable,
        "python": sys.version,
        "platform": platform.platform(),
        "versions": versions,
        "repetitions_per_cell": args.repeats,
        "method": (
            "Fixture-connected hosts warm pytest and pytest_asyncio equally "
            "before initial RSS/timing. Remaining runtime-fixture bookkeeping "
            "makes these instrumented candidates, not pure production RSS. "
            "Fresh -I probes, common host imports warmed before timings; "
            "current psutil RSS bytes. Real SDKs; B3/B4 file-mailbox "
            "platform/network substitutes. Same-process installed "
            "channel.py vs subprocess installed Driver. Readiness includes "
            "SDK/network boundary imports and connect: baseline timer "
            "starts before boundary import after Channel construction; "
            "subprocess timer starts at proxy.start before child "
            "boot/handshake/import. Preparation subprocess includes "
            "fixture/copy overhead explicitly, not comparable end-to-end "
            "speed. Cold experiment warms the same BaseChannel/config/proxy "
            "support in parent and import child; it is a separate Driver "
            "import diagnostic in both modes, excluding network and legacy "
            "Channel startup. OS caches not flushed; summed RSS "
            "double-counts shared pages, not physical RAM or Console boot."
        ),
        "samples": samples,
    }
    path = Path("/tmp/qwenpaw-b5-measurements.json")
    path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(str(path))


if __name__ == "__main__":
    main()
