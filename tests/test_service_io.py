import asyncio
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from civ_agent.service_io import ServiceIO


class ServiceIOTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_cancelled_error_does_not_spin_the_event_loop(self):
        # A subprocess bounds the regression: repeatedly awaiting an already
        # failed future otherwise spins without yielding to asyncio's timers.
        result = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable,
                "-c",
                """
import asyncio
from civ_agent.service_io import ServiceIO

def cancelled():
    raise asyncio.CancelledError()

async def main():
    io = ServiceIO()
    try:
        try:
            await io.call(cancelled)
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError('Expected cancellation')
        assert await io.call(lambda: 'still usable') == 'still usable'
    finally:
        await io.close()

asyncio.run(main())
""",
            ],
            timeout=5,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    async def test_cancellation_and_shutdown_wait_for_the_accepted_write(self):
        worker = ServiceIO()
        entered, release = threading.Event(), threading.Event()
        with tempfile.TemporaryDirectory(prefix="service-io-") as directory:
            path = Path(directory) / "events"

            def write():
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("Test did not release writer")
                path.write_text("accepted event\n")

            task = asyncio.create_task(worker.call(write))
            closing = None
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                task.cancel()
                closing = asyncio.create_task(worker.close())
                await asyncio.sleep(0.05)
                self.assertFalse(task.done())
                self.assertFalse(closing.done())
                task.cancel()
                await asyncio.sleep(0.01)
                self.assertFalse(task.done())
                self.assertFalse(closing.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                await closing
                self.assertEqual("accepted event\n", path.read_text())
            finally:
                release.set()
                await asyncio.gather(task, *([closing] if closing else []), return_exceptions=True)
                await worker.close()
