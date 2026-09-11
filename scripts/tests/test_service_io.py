import asyncio
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from service_io import ServiceIO


class ServiceIOTests(unittest.IsolatedAsyncioTestCase):
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
