#!/usr/bin/env python3
"""SIP <-> OpenAI GPT-Live bridge for the 464646 Santa line (one call at a time)."""
import asyncio
import base64
import json
import os
import random
import re
import struct
import sys
import time
import uuid

import websockets

CONFIG_PATH = "/etc/santa-bridge/config.json"
REQUIRED = ["api_key", "sip_ip", "sip_port", "rtp_port", "voice", "voice_description",
            "max_call_seconds", "goodbye_at_seconds", "barge_in_rms", "transcript_dir",
            "instructions", "greetings", "greeting_index_file"]
LIVE_URL = "wss://api.openai.com/v1/live/sessions"
PT_NAME = {0: "PCMU", 8: "PCMA"}
SILENCE = {0: 0xFF, 8: 0xD5}


def load_config():
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED if k not in cfg]
    if missing:
        sys.exit(f"{CONFIG_PATH}: missing required keys: {', '.join(missing)}")
    if not cfg["greetings"]:
        sys.exit(f"{CONFIG_PATH}: greetings must list at least one greeting")
    return cfg


def next_greeting(cfg):
    path = cfg["greeting_index_file"]
    idx = int(open(path).read().strip()) if os.path.exists(path) else 0
    greeting = cfg["greetings"][idx % len(cfg["greetings"])]
    with open(path, "w") as f:
        f.write(str((idx + 1) % len(cfg["greetings"])))
    return greeting


def ulaw_to_lin(b):
    b = ~b & 0xFF
    t = ((b & 0x0F) << 3) + 0x84
    t <<= (b & 0x70) >> 4
    return (0x84 - t) if (b & 0x80) else (t - 0x84)


def alaw_to_lin(b):
    b ^= 0x55
    t = (b & 0x0F) << 4
    seg = (b & 0x70) >> 4
    t = t + 8 if seg == 0 else (t + 0x108) << (seg - 1)
    return t if (b & 0x80) else -t


DECODE = {0: [ulaw_to_lin(i) for i in range(256)], 8: [alaw_to_lin(i) for i in range(256)]}


def rms(payload, pt):
    tab = DECODE[pt]
    if not payload:
        return 0
    return int((sum(tab[x] * tab[x] for x in payload) / len(payload)) ** 0.5)


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


class SipMsg:
    def __init__(self, data):
        text = data.decode("utf-8", "replace")
        head, _, self.body = text.partition("\r\n\r\n")
        lines = head.split("\r\n")
        self.start = lines[0]
        self.headers = []
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                self.headers.append((k.strip(), v.strip()))
        self.is_request = not self.start.startswith("SIP/2.0")
        self.method = self.start.split()[0] if self.is_request else None

    def get(self, name, all_=False):
        short = {"via": "v", "from": "f", "to": "t", "call-id": "i", "contact": "m",
                 "content-type": "c", "content-length": "l", "cseq": "cseq"}
        names = {name.lower(), short.get(name.lower(), "")}
        vals = [v for k, v in self.headers if k.lower() in names]
        return vals if all_ else (vals[0] if vals else None)


class Call:
    def __init__(self, bridge, invite, addr):
        self.bridge = bridge
        self.cfg = bridge.cfg
        self.invite = invite
        self.addr = addr
        self.call_id = invite.get("Call-ID")
        self.local_tag = uuid.uuid4().hex[:10]
        self.ended = False
        self.pt = None
        self.remote_rtp = None
        self.out_q = bytearray()
        self.loud_frames = 0
        self.transcript = []
        self.cseq = random.randint(1000, 9000)
        self.started = time.monotonic()
        self.ws = None
        self.last_rx = 0.0

    def parse_sdp(self):
        ip = port = None
        offered = []
        for ln in self.invite.body.splitlines():
            if ln.startswith("c=IN IP4 "):
                ip = ln.split()[2]
            elif ln.startswith("m=audio "):
                parts = ln.split()
                port = int(parts[1])
                offered = [int(p) for p in parts[3:] if p.isdigit()]
        pt = next((p for p in offered if p in PT_NAME), None)
        return ip, port, pt

    def response(self, code, reason, body=""):
        inv = self.invite
        to = inv.get("To")
        if code != 100 and ";tag=" not in to:
            to += f";tag={self.local_tag}"
        hdrs = [f"SIP/2.0 {code} {reason}"]
        hdrs += [f"Via: {v}" for v in inv.get("Via", all_=True)]
        hdrs += [f"From: {inv.get('From')}", f"To: {to}", f"Call-ID: {self.call_id}",
                 f"CSeq: {inv.get('CSeq')}",
                 f"Contact: <sip:santa@{self.cfg['sip_ip']}:{self.cfg['sip_port']}>",
                 "User-Agent: santa-bridge", "Allow: INVITE, ACK, BYE, CANCEL, OPTIONS"]
        if body:
            hdrs.append("Content-Type: application/sdp")
        hdrs.append(f"Content-Length: {len(body)}")
        return ("\r\n".join(hdrs) + "\r\n\r\n" + body).encode()

    def answer_sdp(self):
        ip, port, name = self.cfg["sip_ip"], self.cfg["rtp_port"], PT_NAME[self.pt]
        sid = int(time.time())
        return (f"v=0\r\no=santa {sid} {sid} IN IP4 {ip}\r\ns=santa\r\nc=IN IP4 {ip}\r\n"
                f"t=0 0\r\nm=audio {port} RTP/AVP {self.pt}\r\na=rtpmap:{self.pt} {name}/8000\r\n"
                f"a=ptime:20\r\na=sendrecv\r\n")

    def send_bye(self):
        inv = self.invite
        contact = inv.get("Contact")
        m = re.search(r"<([^>]+)>", contact) if contact else None
        target = m.group(1) if m else re.search(r"<([^>]+)>", inv.get("From")).group(1)
        self.cseq += 1
        branch = "z9hG4bK" + uuid.uuid4().hex[:12]
        to = inv.get("From")
        frm = inv.get("To")
        if ";tag=" not in frm:
            frm += f";tag={self.local_tag}"
        msg = (f"BYE {target} SIP/2.0\r\n"
               f"Via: SIP/2.0/UDP {self.cfg['sip_ip']}:{self.cfg['sip_port']};branch={branch};rport\r\n"
               f"Max-Forwards: 70\r\nFrom: {frm}\r\nTo: {to}\r\nCall-ID: {self.call_id}\r\n"
               f"CSeq: {self.cseq} BYE\r\nUser-Agent: santa-bridge\r\nContent-Length: 0\r\n\r\n")
        self.bridge.sip.sendto(msg.encode(), self.addr)

    async def run(self):
        ip, port, pt = self.parse_sdp()
        if pt is None:
            self.bridge.sip.sendto(self.response(488, "Not Acceptable Here"), self.addr)
            log(self.call_id, "no PCMU/PCMA offered")
            return
        self.pt, self.remote_rtp = pt, (ip, port)
        self.bridge.sip.sendto(self.response(100, "Trying"), self.addr)
        self.bridge.sip.sendto(self.response(200, "OK", self.answer_sdp()), self.addr)
        log(self.call_id, "answered", PT_NAME[pt], "rtp->", self.remote_rtp,
            "from", self.invite.get("From"))
        tasks = [asyncio.create_task(self.rtp_sender())]
        try:
            await self.openai_session(tasks)
        except Exception as e:
            log(self.call_id, "openai session failed:", repr(e))
        finally:
            for t in tasks:
                t.cancel()
            if not self.ended:
                self.ended = True
                self.send_bye()
            self.write_transcript()
            log(self.call_id, "ended after", int(time.monotonic() - self.started), "s")

    async def openai_session(self, tasks):
        cfg = self.cfg
        fmt = {"type": "audio/pcmu" if self.pt == 0 else "audio/pcma", "rate": 8000}
        greeting = ("Greet the caller now in English, in character as Santa, saying close to this: "
                    f"\"{next_greeting(cfg)}\" Then pause and listen.")
        instructions = cfg["instructions"]
        if cfg["voice_description"]:
            instructions += "\n\nVoice: " + cfg["voice_description"]
        start = {
            "type": "session.start", "event_id": "start",
            "session": {
                "model": "gpt-live-1",
                "instructions": instructions,
                "audio": {"format": fmt, "output": {"voice": cfg["voice"]}},
                "delegation": {"type": "client"},
                "input": [{"type": "message", "role": "developer",
                           "content": [{"type": "input_text", "text": greeting}]}],
            },
        }
        async with websockets.connect(LIVE_URL, additional_headers={
                "Authorization": f"Bearer {cfg['api_key']}"}, max_size=None) as ws:
            self.ws = ws
            await ws.send(json.dumps(start))
            goodbye_sent = False
            async for raw in ws:
                ev = json.loads(raw)
                t = ev.get("type")
                if t == "session.started":
                    log(self.call_id, "openai session", ev.get("session", {}).get("id"))
                    await ws.send(json.dumps({"type": "session.commentary.append",
                                              "event_id": "greet", "delegation_id": None,
                                              "content": greeting}))
                    tasks.append(asyncio.create_task(self.timer(ws)))
                elif t == "session.output_audio.delta":
                    self.out_q += base64.b64decode(ev["delta"])
                elif t == "session.input_transcript.delta":
                    self.add_transcript("child", ev.get("delta", ""))
                elif t == "session.output_transcript.delta":
                    self.add_transcript("santa", ev.get("delta", ""))
                elif t == "error":
                    log(self.call_id, "openai error:", json.dumps(ev.get("error")))
                elif t == "session.closed":
                    log(self.call_id, "openai usage:", json.dumps(ev.get("usage")))
                    return
                if self.ended:
                    return

    async def timer(self, ws):
        cfg = self.cfg
        await asyncio.sleep(cfg["goodbye_at_seconds"])
        if self.ended:
            return
        await ws.send(json.dumps({"type": "session.commentary.append", "event_id": "bye",
                                  "delegation_id": None,
                                  "content": "Time is nearly up. Right now, say a short, warm "
                                             "goodbye to the child and wish them a merry Christmas."}))
        await asyncio.sleep(cfg["max_call_seconds"] - cfg["goodbye_at_seconds"])
        while self.out_q and not self.ended:
            await asyncio.sleep(0.1)
        self.hangup()

    def hangup(self):
        if self.ended:
            return
        self.ended = True
        self.send_bye()
        if self.ws is not None:
            asyncio.create_task(self.close_ws())

    async def close_ws(self):
        try:
            await self.ws.send(json.dumps({"type": "session.close"}))
            await asyncio.wait_for(self.ws.wait_closed(), 10)
        except Exception:
            await self.ws.close()

    def add_transcript(self, who, text):
        if self.transcript and self.transcript[-1][0] == who:
            self.transcript[-1][1] += text
        else:
            self.transcript.append([who, text])

    def write_transcript(self):
        if not self.transcript:
            return
        d = self.cfg["transcript_dir"]
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, time.strftime("%Y%m%d-%H%M%S") + f"-{self.call_id[:8]}.txt")
        with open(path, "w") as f:
            for who, text in self.transcript:
                f.write(f"{who}: {text.strip()}\n")
        log(self.call_id, "transcript", path)

    def on_rtp(self, data):
        if len(data) < 12 or self.ended:
            return
        cc = data[0] & 0x0F
        payload = data[12 + 4 * cc:]
        self.last_rx = time.monotonic()
        if self.out_q:
            self.loud_frames = self.loud_frames + 1 if rms(payload, self.pt) > self.cfg["barge_in_rms"] else 0
            if self.loud_frames >= 3:
                self.out_q.clear()
                self.loud_frames = 0
        if self.ws is not None and payload:
            asyncio.create_task(self.ws.send(json.dumps({
                "type": "session.input_audio.append",
                "audio": base64.b64encode(payload).decode()})))

    async def rtp_sender(self):
        seq, ts, ssrc = random.randint(0, 65535), random.randint(0, 2**31), random.getrandbits(32)
        nxt = time.monotonic()
        silence = bytes([SILENCE[self.pt]]) * 160
        while not self.ended:
            frame = bytes(self.out_q[:160])
            del self.out_q[:160]
            if len(frame) < 160:
                frame += silence[len(frame):]
            hdr = struct.pack("!BBHII", 0x80, self.pt, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc)
            self.bridge.rtp.sendto(hdr + frame, self.remote_rtp)
            # GPT-Live needs continuous input audio, including silence before the caller speaks.
            if self.ws is not None and time.monotonic() - self.last_rx > 0.04:
                asyncio.create_task(self.ws.send(json.dumps({
                    "type": "session.input_audio.append",
                    "audio": base64.b64encode(silence).decode()})))
            seq += 1
            ts += 160
            nxt += 0.02
            await asyncio.sleep(max(0, nxt - time.monotonic()))


class SipProto(asyncio.DatagramProtocol):
    def __init__(self, bridge):
        self.bridge = bridge

    def datagram_received(self, data, addr):
        self.bridge.on_sip(data, addr)


class RtpProto(asyncio.DatagramProtocol):
    def __init__(self, bridge):
        self.bridge = bridge

    def datagram_received(self, data, addr):
        if self.bridge.call:
            self.bridge.call.on_rtp(data)


class Bridge:
    def __init__(self, cfg):
        self.cfg = cfg
        self.call = None
        self.sip = self.rtp = None

    def reply(self, msg, addr, code, reason):
        hdrs = [f"SIP/2.0 {code} {reason}"] + [f"Via: {v}" for v in msg.get("Via", all_=True)]
        to = msg.get("To")
        if ";tag=" not in to:
            to += f";tag={uuid.uuid4().hex[:10]}"
        hdrs += [f"From: {msg.get('From')}", f"To: {to}", f"Call-ID: {msg.get('Call-ID')}",
                 f"CSeq: {msg.get('CSeq')}", "Content-Length: 0"]
        self.sip.sendto(("\r\n".join(hdrs) + "\r\n\r\n").encode(), addr)

    def on_sip(self, data, addr):
        msg = SipMsg(data)
        if not msg.is_request:
            return
        cid = msg.get("Call-ID")
        cur = self.call if self.call and self.call.call_id == cid else None
        if msg.method == "INVITE":
            if cur:
                return
            if self.call and not self.call.ended:
                self.reply(msg, addr, 486, "Busy Here")
                log(cid, "busy: call already active")
                return
            self.call = Call(self, msg, addr)
            asyncio.create_task(self.call.run())
        elif msg.method == "BYE":
            self.reply(msg, addr, 200, "OK")
            if cur and not cur.ended:
                log(cid, "caller hung up")
                cur.ended = True
                if cur.ws is not None:
                    asyncio.create_task(cur.close_ws())
        elif msg.method == "CANCEL":
            self.reply(msg, addr, 200, "OK")
            if cur:
                cur.ended = True
        elif msg.method == "OPTIONS":
            self.reply(msg, addr, 200, "OK")
        elif msg.method != "ACK":
            self.reply(msg, addr, 405, "Method Not Allowed")

    async def main(self):
        loop = asyncio.get_running_loop()
        self.sip, _ = await loop.create_datagram_endpoint(
            lambda: SipProto(self), local_addr=(self.cfg["sip_ip"], self.cfg["sip_port"]))
        self.rtp, _ = await loop.create_datagram_endpoint(
            lambda: RtpProto(self), local_addr=(self.cfg["sip_ip"], self.cfg["rtp_port"]))
        log("santa-bridge listening sip", f"{self.cfg['sip_ip']}:{self.cfg['sip_port']}",
            "rtp", self.cfg["rtp_port"], "voice", json.dumps(self.cfg["voice"]),
            "greetings", len(self.cfg["greetings"]))
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(Bridge(load_config()).main())
