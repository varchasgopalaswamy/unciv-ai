"""Forward the local browser port to the desktop's internal noVNC service."""

import asyncio
import sys


async def copy_stream(reader, writer):
    while data := await reader.read(65536):
        writer.write(data)
        await writer.drain()
    writer.write_eof()


async def serve(target):
    async def forward(browser_reader, browser_writer):
        try:
            desktop_reader, desktop_writer = await asyncio.open_connection(target, 6080)
            try:
                await asyncio.gather(
                    copy_stream(browser_reader, desktop_writer),
                    copy_stream(desktop_reader, browser_writer),
                )
            finally:
                desktop_writer.close()
        except OSError:
            pass  # A browser disconnect or controller restart only affects this connection.
        finally:
            browser_writer.close()

    async with await asyncio.start_server(forward, "0.0.0.0", 6080) as server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(serve(sys.argv[1]))
