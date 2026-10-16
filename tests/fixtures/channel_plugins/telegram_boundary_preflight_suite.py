"""External fixture sanity with native SDK, separate from feature proof."""

import json
import os
import subprocess

from telegram_support import TOKEN, fault_runtime, push, rows, telegram_sdk


def test_http_service_produces_native_updates_files_multipart_and_close(
    telegram_sdk, tmp_path
):
    push(telegram_sdk.work, text="native fixture input")
    code = """
import asyncio,sys
sys.path.insert(0, sys.argv[1])
from telegram import Bot, InputFile
async def main():
    bot=Bot('123:local-A')
    await bot.initialize()
    updates=await bot.get_updates(timeout=10, read_timeout=20)
    assert updates[0].message.text=='native fixture input'
    assert updates[0].message.from_user.id==456
    message=await bot.send_message(12345,'native fixture reply')
    assert message.text=='native fixture reply'
    await bot.send_document(12345,InputFile(b'pdf-data',filename='report.pdf'))
    file=await bot.get_file('largest')
    data=await file.download_as_bytearray()
    assert bytes(data)==b'downloaded media bytes'
    await bot.shutdown()
asyncio.run(main())
"""
    result = subprocess.run(
        [str(telegram_sdk.python), "-I", "-c", code, str(telegram_sdk.site)],
        env={
            **os.environ,
            "QWENPAW_WORKING_DIR": str(tmp_path / "host"),
            "QWENPAW_SECRET_DIR": str(tmp_path / "secret"),
            "QWENPAW_BACKUP_DIR": str(tmp_path / "backup"),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert rows(telegram_sdk.work, "getUpdates")[0]["read_timeout"] == 30
    assert rows(telegram_sdk.work, "sendDocument")[0]["files"]["document"] == {
        "filename": "report.pdf",
        "mime": "application/pdf",
        "bytes_b64": "cGRmLWRhdGE=",
    }
    assert len(rows(telegram_sdk.work, "http-close-exit")) == 2
