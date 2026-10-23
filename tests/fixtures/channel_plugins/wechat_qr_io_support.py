"""Thread barriers at actual QR filesystem boundaries."""

import asyncio
import threading


class FileIOGate:
    """Hold one actual filesystem boundary without blocking the event loop."""

    def __init__(self, monkeypatch, owner, name, matches):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.thread_ids = []
        self.loop_thread = threading.get_ident()
        original = getattr(owner, name)

        def blocked(*args, **kwargs):
            if matches(*args, **kwargs):
                self.calls += 1
                self.thread_ids.append(threading.get_ident())
                assert (
                    threading.get_ident() != self.loop_thread
                ), "actual QR filesystem I/O ran on the event loop"
                self.entered.set()
                assert self.release.wait(
                    10
                ), "QR filesystem gate was not released"
            return original(*args, **kwargs)

        monkeypatch.setattr(owner, name, blocked)

    async def wait(self, task):
        async with asyncio.timeout(5):
            while not self.entered.is_set() and not task.done():
                await asyncio.sleep(0.001)
        if not self.entered.is_set():
            await task
            raise AssertionError(
                "actual QR filesystem boundary was not reached"
            )
        turns = []
        asyncio.get_running_loop().call_soon(turns.append, True)
        await asyncio.sleep(0)
        assert turns == [True] and not task.done()
        assert all(t != self.loop_thread for t in self.thread_ids)
