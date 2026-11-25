# santa-bridge

The full story of this project is on the blog: [Payphone Christmas Project](https://nickvsnetworking.com/payphone-christmas-project/).

A phone line to Santa. `santa-bridge` answers a SIP call and connects the caller to an
OpenAI GPT-Live voice session playing Father Christmas, so a child can pick up a phone,
dial a number and have a real conversation with Santa.

It was built for a payphone on Norfolk Island, dialled as `464646` (HOHOHO on the keypad),
and the example config holds the Norfolk Island prompts and greetings.

```
  phone ── SIP switch / PBX ──── SIP + G.711 RTP ────> santa-bridge ──── WebSocket ────> GPT-Live
                                 (PCMU or PCMA)                          (audio/pcmu or audio/pcma, 8 kHz)
```

## What it does

- A single Python file with one dependency (`websockets`). It handles SIP over UDP (INVITE,
  ACK, BYE, CANCEL, OPTIONS) and RTP itself. One call at a time; a second caller gets
  `486 Busy Here`.
- Passes G.711 audio straight through to GPT-Live in both directions, with no transcoding.
- Plays R2 MFC signalling tones and ringback as early media while GPT-Live connects, then
  answers and plays the next greeting at once, from a recording made with
  `generate_greetings.py` in the same voice. Greetings are used in rotation.
- Barge-in: when the caller talks over Santa, queued Santa audio is dropped
  (`barge_in_rms`), so he stops and listens.
- Says a warm goodbye at `goodbye_at_seconds` and hangs up at `max_call_seconds`.
- Writes a text transcript of each call to `transcript_dir`.

## Setup

```sh
sudo useradd --system --no-create-home --shell /usr/sbin/nologin santa
sudo mkdir -p /opt/santa-bridge
sudo cp santa_bridge.py /opt/santa-bridge/
sudo python3 -m venv /opt/santa-bridge/venv
sudo /opt/santa-bridge/venv/bin/pip install -r requirements.txt

sudo install -d -o root -g santa -m 750 /etc/santa-bridge
sudo install -o root -g santa -m 640 config.example.json /etc/santa-bridge/config.json
sudoedit /etc/santa-bridge/config.json      # set api_key, sip_ip and voice

sudo cp generate_greetings.py /opt/santa-bridge/
cd /opt/santa-bridge && sudo -u santa venv/bin/python generate_greetings.py   # again after changing greetings or voice

sudo cp santa-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now santa-bridge
journalctl -u santa-bridge -f
```

Then route a number on your SIP switch to `sip:santa@<sip_ip>:<sip_port>`. For FreeSWITCH:

```xml
<extension name="Santa">
  <condition field="destination_number" expression="^464646$">
    <action application="bridge" data="{absolute_codec_string='PCMU,PCMA'}sofia/internal/sip:santa@192.0.2.10:5090" />
  </condition>
</extension>
```

The bridge has no registration or authentication. Bind it to an address only your switch
can reach.

## Configuration

All settings are in `/etc/santa-bridge/config.json`. Every key is required; the bridge
exits at startup naming any that are missing.

| Key | Meaning |
|---|---|
| `api_key` | OpenAI API key |
| `sip_ip` | Local address to listen on, also used in SDP and Contact |
| `sip_port` / `rtp_port` | SIP and RTP UDP ports |
| `voice` | GPT-Live output voice, e.g. `{"id": "voice_..."}` for a custom voice |
| `voice_description` | Description of the voice, appended to the instructions for voice performance |
| `instructions` | Santa's system prompt |
| `greetings` | Opening lines, used in rotation |
| `greeting_index_file` | Where the next greeting number is kept |
| `max_call_seconds` / `goodbye_at_seconds` | Hard call limit, and when Santa starts saying goodbye |
| `barge_in_rms` | Caller loudness (linear RMS) that interrupts Santa |
| `transcript_dir` | Where call transcripts go |

### The voice

The `voice` in the example is a custom voice made from a text prompt. Voice IDs belong to
the OpenAI organisation that made them, so make your own and put its ID in `voice`:

```sh
curl https://api.openai.com/v1/audio/voices \
  -H "Authorization: Bearer $OPENAI_API_KEY" -H "Content-Type: application/json" \
  -d '{"type": "prompt", "name": "Santa", "model": "auto",
       "prompt": "<the voice_description from config.example.json>"}'
```

Change the accent line at the end of `voice_description` (for example "soft Southern
British English") and create a new voice for a different accent.

## Cost

GPT-Live is billed per minute of session. With the defaults, one call is at most 5 minutes.
