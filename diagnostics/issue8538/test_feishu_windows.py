"""Temporary issue #8538 diagnostic. Never merge this into the test suite."""
import asyncio
from dataclasses import replace
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import time
import traceback

from aiohttp import web
import pytest
import pytest_asyncio
from tests.channel_support.host import Host, KEY
from tests.channel_support.process import ROOT

RESULTS = Path(os.environ.get('ISSUE8538_OUTPUT', 'diagnostic-output'))


def record(kind, **fields):
    RESULTS.mkdir(exist_ok=True, parents=True)
    row = {'kind': kind, 't': time.monotonic(), **fields}
    with (RESULTS / 'events.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(row, ensure_ascii=True) + '\n')
    print('ISSUE8538 ' + json.dumps(row), flush=True)


def exception_chain(error):
    rows, seen = [], set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        rows.append({'type': type(error).__name__, 'message': str(error)})
        error = error.__cause__ or error.__context__
    return rows


class LocalFeishu:
    def __init__(self, mode):
        self.mode = mode
        self.url = ''
        self.sockets = []

    async def handle(self, request):
        path = request.path
        record('server_request', mode=self.mode, path=path)
        if path == '/open-apis/auth/v3/tenant_access_token/internal':
            return web.json_response({'code': 0, 'msg': 'ok', 'tenant_access_token': 'synthetic-diagnostic-token', 'expire': 7200})
        if path == '/open-apis/bot/v3/info':
            return web.json_response({'code': 0, 'msg': 'ok', 'bot': {'open_id': 'ou_diagnostic', 'app_name': 'diagnostic'}})
        if path == '/callback/ws/endpoint':
            return web.json_response({'code': 0, 'msg': 'ok', 'data': {'URL': self.url.replace('http://', 'ws://') + '/ws?device_id=diagnostic&service_id=1', 'ClientConfig': {'PingInterval': 30, 'ReconnectInterval': 1, 'ReconnectCount': 0, 'ReconnectNonce': 0}}})
        if path == '/ws':
            record('websocket_tcp_accepted', mode=self.mode)
            if self.mode == 'slow-handshake':
                await asyncio.sleep(12)
            if self.mode == 'rejected-handshake':
                return web.Response(status=403, text='Synthetic diagnostic handshake rejection')
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            self.sockets.append(socket)
            record('websocket_upgraded', mode=self.mode)
            async for _message in socket:
                pass
            return socket
        return web.json_response({'code': 999999, 'msg': 'unexpected synthetic endpoint'}, status=404)


@pytest_asyncio.fixture
async def host(tmp_path, monkeypatch):
    case = await Host.create(tmp_path, monkeypatch)
    case.registry.channel_runtime = replace(case.registry.channel_runtime, startup_timeout=10.0, shutdown_timeout=2.0, connection_timeout=None)
    from qwenpaw.app.channels.isolated_proxy import IsolatedChannelProxy
    original = IsolatedChannelProxy._wait_connected
    async def observed(self):
        started = time.monotonic()
        record('wait_connected_begin', instance=self._launch_spec.instance_id, budget=self._launch_spec.startup_timeout)
        try:
            result = await original(self)
        except BaseException as error:
            health = self.channel_process.health_snapshot()
            record('wait_connected_error', elapsed=time.monotonic()-started, chain=exception_chain(error), state=health.get('state'), platform_state=health.get('platform_state'), last_error=health.get('last_error'), pid=health.get('pid'))
            traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)
            raise
        else:
            health = self.channel_process.health_snapshot()
            record('wait_connected_ok', elapsed=time.monotonic()-started, state=health.get('state'), platform_state=health.get('platform_state'), pid=health.get('pid'))
            return result
    monkeypatch.setattr(IsolatedChannelProxy, '_wait_connected', observed)
    try:
        yield case
    finally:
        try:
            await case.close()
        except BaseException as error:
            record('cleanup_error', chain=exception_chain(error))
            raise


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['normal', 'rejected-handshake', 'slow-handshake'])
async def test_real_feishu_worker(host, mode):
    record('environment', scenario=mode, python=sys.version, platform=platform.platform(), loop=type(asyncio.get_running_loop()).__name__, versions={name: importlib.metadata.version(name) for name in ('lark-oapi','websockets','httpx')})
    service = LocalFeishu(mode)
    app = web.Application()
    app.router.add_route('*', '/{path:.*}', service.handle)
    runner = web.AppRunner(app, shutdown_timeout=0.2)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    service.url = 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])
    source = host.root / 'incoming' / 'feishu'
    shutil.copytree(ROOT / 'plugins/channel/feishu', source)
    # This is the actual shipped plugin, not the generic sample component.
    descriptor = json.loads((source/'channel.json').read_text(encoding='utf-8'))
    assert descriptor['entrypoint']['module'] == 'qwenpaw_channel_feishu.driver'
    ws = await host.workspace('diagnostic', app_id='diagnostic-'+mode, app_secret='synthetic-local-only-secret', domain=service.url)
    result = None
    try:
        try:
            result = await host.loader.load_plugin_from_path(source)
        except BaseException as error:
            record('activation_exception', scenario=mode, chain=exception_chain(error))
            traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)
        channel = await ws.channel_manager.get_channel(KEY)
        health = await channel.health_check() if channel is not None else None
        record('activation_result', scenario=mode, status=getattr(result,'status',None), diagnostics=getattr(result,'diagnostics',None), registered=host.registry.get_channel_registration(KEY) is not None, health=({key: health.get(key) for key in ('status','state','platform_state','last_error','pid')} if health else None))
        if mode == 'normal':
            assert health is not None and health['status']=='healthy', 'Real Feishu SDK did not connect to the local service on Windows'
        else:
            assert channel is None, 'Injected failed/slow handshake should not appear healthy'
    finally:
        if host.loader.get_loaded_plugin(KEY):
            await host.loader.unload_plugin(KEY)
        for socket in service.sockets:
            await socket.close()
        await runner.cleanup()
