from pathlib import Path
p = Path('src/qwenpaw/plugins/execution/process_tree.py')
s = p.read_text().replace('    if os.name == "nt":\n        _watch_windows_parent(parent_pid)', '    import sys\n    print(f"DIAGNOSTIC worker pid={os.getpid()} ppid={os.getppid()} host={parent_pid}", file=sys.stderr, flush=True)\n    if os.name == "nt":\n        _watch_windows_parent(parent_pid)')
p.write_text(s)
p = Path('src/qwenpaw/plugins/execution/process.py')
s = p.read_text().replace('            self._tree = ProcessTree(self._process.pid)', '''            self._tree = ProcessTree(self._process.pid)
            import ctypes
            tree, child = self._tree, self._process
            def diagnostic_job():
                if tree._job is None:
                    return
                data = (ctypes.c_size_t * 32)()
                ok = tree._kernel.QueryInformationJobObject(tree._job, 3, ctypes.byref(data), ctypes.sizeof(data), None)
                print(f"DIAGNOSTIC host={os.getpid()} leader={child.pid} job_ok={ok} job_pids={list(data)} exit={child.returncode}", file=sys.stderr, flush=True)
            asyncio.get_running_loop().call_later(0.2, diagnostic_job)''')
# Do not dump worker stderr, as byte tests depend on exactly existing logger behavior.
p.write_text(s)
p = Path('tests/channel_support/worker.py')
s = p.read_text().replace('        if mode == "tree":\n', '''        if mode == "tree":
            import faulthandler, traceback, ctypes, msvcrt
            trace = open(str(marker) + '.trace', 'w', buffering=1)
            print('entered tree', file=trace)
            faulthandler.dump_traceback_later(2, file=trace)
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.GetStdHandle.argtypes = [ctypes.c_ulong]
            kernel.GetStdHandle.restype = ctypes.c_void_p
            for fd, std in [(0, -10), (1, -11), (2, -12)]:
                print('HANDLE', fd, msvcrt.get_osfhandle(fd), kernel.GetStdHandle(std), file=trace)
''')
s = s.replace('            child = subprocess.Popen(', '            print("before Popen", file=trace)\n            child = subprocess.Popen(')
s = s.replace('            while not Path(ready).exists():', '            print("after Popen", child.pid, file=trace)\n            while not Path(ready).exists():\n                if child.poll() is not None:\n                    print("CHILD EXIT", child.returncode, file=trace)\n                    break')
s = s.replace('            marker.write_text(str(child.pid))', '            print("ready?", Path(ready).exists(), file=trace)\n            marker.write_text(str(child.pid))\n            faulthandler.cancel_dump_traceback_later()')
p.write_text(s)
p=Path('tests/unit/plugins/test_plugin_process.py')
s=p.read_text().replace('    await eventually(marker.exists)\n    descendant = int(marker.read_text())', '''    try:
        await eventually(marker.exists)
    finally:
        trace = Path(str(marker) + '.trace')
        print('TREE TRACE', trace.read_text() if trace.exists() else 'NO TRACE', process.health_snapshot(), flush=True)
    descendant = int(marker.read_text())''',1)
p.write_text(s)
