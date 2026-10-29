from pathlib import Path

p = Path('src/qwenpaw/plugins/execution/process_tree.py')
s = p.read_text()
s = s.replace('    def watch() -> None:\n', '''    import sys
    print(f"DIAGNOSTIC worker pid={os.getpid()} ppid={os.getppid()} host={parent_pid}", file=sys.stderr, flush=True)
    def watch() -> None:
''')
s = s.replace('                os._exit(71)', '                print("DIAGNOSTIC parent mismatch exit=71", file=sys.stderr, flush=True)\n                os._exit(71)')
p.write_text(s)
p = Path('src/qwenpaw/plugins/execution/worker.py')
s = p.read_text().replace('        except Exception:\n            await runtime.state(context, runtime.failed_state)', '        except Exception:\n            import traceback\n            traceback.print_exc()\n            await runtime.state(context, runtime.failed_state)')
p.write_text(s)
p = Path('src/qwenpaw/plugins/execution/process.py')
s = p.read_text().replace('            self._tree = ProcessTree(self._process.pid)', '''            self._tree = ProcessTree(self._process.pid)
            import ctypes
            def diagnostic_job():
                data = (ctypes.c_size_t * 32)()
                ok = self._tree._kernel.QueryInformationJobObject(self._tree._job, 3, ctypes.byref(data), ctypes.sizeof(data), None)
                print(f"DIAGNOSTIC host={os.getpid()} leader={self._process.pid} job_ok={ok} job_pids={list(data)} exit={self._process.returncode}", flush=True)
            asyncio.get_running_loop().call_later(0.2, diagnostic_job)''')
# Print test-only worker stderr in addition to normal bounded log handling.
s = s.replace('            self._log_bytes += len(chunk)', '            print("DIAGNOSTIC STDERR", chunk.decode("utf-8", errors="replace"), flush=True)\n            self._log_bytes += len(chunk)')
p.write_text(s)
