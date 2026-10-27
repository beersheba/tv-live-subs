"""Stream a 16 kHz mono WAV to the server at real-time pace and print the results.

    uv run python test_client.py sample.wav --src he --dst ru
"""
import argparse
import asyncio
import json
import time
import wave

from websockets.asyncio.client import connect


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("wav")
    p.add_argument("--url", default="ws://localhost:8765")
    p.add_argument("--src", default="he")
    p.add_argument("--dst", default="ru")
    p.add_argument("--tail-silence", type=float, default=2.0)
    p.add_argument("--speed", type=float, default=1.0, help="replay faster than real time")
    a = p.parse_args()

    with wave.open(a.wav) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2
        pcm = w.readframes(w.getnframes()) + b"\0\0" * int(16000 * a.tail_silence)

    async with connect(a.url) as ws:
        await ws.send(json.dumps({"type": "config", "src": a.src, "dst": a.dst}))
        t0 = time.monotonic()

        async def reader() -> None:
            async for m in ws:
                d = json.loads(m)
                if d["type"] == "commit":
                    print(f"[{time.monotonic() - t0:5.1f}s] #{d['id']} {d['text']}\n"
                          f"{'':9}-> {d['translation']}", flush=True)

        r = asyncio.create_task(reader())
        chunk = 3200  # 100 ms
        for i in range(0, len(pcm), chunk):
            await ws.send(pcm[i:i + chunk])
            await asyncio.sleep(0.1 / a.speed)
        await asyncio.sleep(6)
        await ws.close()
        await r


asyncio.run(main())
