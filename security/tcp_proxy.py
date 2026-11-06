"""Minimal TCP forwarder for the deployment gateway (deploy/compose.yaml).

The assistant sits on an `internal` Docker network with no route out. Published ports do not work on
internal networks, so this forwarder - the only container on both networks - publishes the UI on
127.0.0.1 and relays bytes to the assistant. It contains no application logic and accepts one target only.

    python -m security.tcp_proxy --listen 0.0.0.0:8765 --target assistant:8765
"""

import argparse
import asyncio


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()


async def main(listen: str, target: str):
    lhost, lport = listen.rsplit(":", 1)
    thost, tport = target.rsplit(":", 1)

    async def handle(client_r, client_w):
        try:
            up_r, up_w = await asyncio.open_connection(thost, int(tport))
        except OSError:
            client_w.close()
            return
        await asyncio.gather(_pipe(client_r, up_w), _pipe(up_r, client_w))

    server = await asyncio.start_server(handle, lhost, int(lport))
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", required=True)
    ap.add_argument("--target", required=True)
    args = ap.parse_args()
    asyncio.run(main(args.listen, args.target))
