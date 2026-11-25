#!/usr/bin/env python3
"""Record each configured greeting once from GPT-Live, in the configured voice, as raw G.711 u-law."""
import asyncio
import base64
import json
import os
import sys

import websockets

from santa_bridge import (DECODE, GREETING_DIR, GREETING_MANIFEST, LIVE_URL, greeting_path,
                          load_config)

FRAME = 160
SILENCE = base64.b64encode(b"\xff" * FRAME).decode()
LOUD_RMS = 300
END_SILENCE_FRAMES = 75
MAX_FRAMES = 1500


def loud(frame):
    tab = DECODE[0]
    return (sum(tab[x] * tab[x] for x in frame) / len(frame)) ** 0.5 > LOUD_RMS


async def record(cfg, text):
    instructions = cfg["instructions"]
    if cfg["voice_description"]:
        instructions += "\n\nVoice: " + cfg["voice_description"]
    say = (f"Say exactly these words, in character as Santa, and nothing else: \"{text}\" "
           "Then stop and stay silent.")
    start = {
        "type": "session.start", "event_id": "start",
        "session": {
            "model": "gpt-live-1",
            "instructions": instructions,
            "audio": {"format": {"type": "audio/pcmu", "rate": 8000}, "output": {"voice": cfg["voice"]}},
            "delegation": {"type": "client"},
            "input": [{"type": "message", "role": "developer",
                       "content": [{"type": "input_text", "text": say}]}],
        },
    }
    audio = bytearray()
    said = []
    async with websockets.connect(LIVE_URL, additional_headers={
            "Authorization": f"Bearer {cfg['api_key']}"}, max_size=None) as ws:
        await ws.send(json.dumps(start))

        async def feed_silence():
            while True:
                await ws.send(json.dumps({"type": "session.input_audio.append", "audio": SILENCE}))
                await asyncio.sleep(0.02)

        feeder = asyncio.create_task(feed_silence())
        first = last = None
        try:
            async for raw in ws:
                ev = json.loads(raw)
                t = ev.get("type")
                if t == "session.started":
                    await ws.send(json.dumps({"type": "session.commentary.append", "event_id": "greet",
                                              "delegation_id": None, "content": say}))
                elif t == "session.output_audio.delta":
                    audio += base64.b64decode(ev["delta"])
                    for n in range((last or 0) // FRAME, len(audio) // FRAME):
                        if loud(audio[n * FRAME:(n + 1) * FRAME]):
                            first = n if first is None else first
                            last = n
                    frames = len(audio) // FRAME
                    if last is not None and frames - last > END_SILENCE_FRAMES:
                        break
                    if frames > MAX_FRAMES:
                        sys.exit(f"no complete greeting after {MAX_FRAMES // 50} s: {text!r}")
                elif t == "session.output_transcript.delta":
                    said.append(ev.get("delta", ""))
                elif t == "error":
                    sys.exit(f"openai error: {json.dumps(ev.get('error'))}")
        finally:
            feeder.cancel()
            await ws.send(json.dumps({"type": "session.close"}))
    return bytes(audio[max(0, first - 5) * FRAME:(last + 15) * FRAME]), "".join(said).strip()


async def main():
    cfg = load_config(check_greeting_audio=False)
    os.makedirs(GREETING_DIR, exist_ok=True)
    manifest = {}
    for i, text in enumerate(cfg["greetings"]):
        audio, said = await record(cfg, text)
        with open(greeting_path(i), "wb") as f:
            f.write(audio)
        manifest[str(i)] = {"text": text, "voice": cfg["voice"]}
        print(f"greeting {i}: {len(audio) / 8000:.1f} s -> {greeting_path(i)}\n  said: {said}", flush=True)
    with open(GREETING_MANIFEST, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"wrote {GREETING_MANIFEST}")


if __name__ == "__main__":
    asyncio.run(main())
