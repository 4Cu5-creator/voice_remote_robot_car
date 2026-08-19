# Voice Remote Robot Car

A voice-controlled robot car: speak a command into a Raspberry Pi Zero 2 W with a
[PiSugar WhisPlay HAT](https://docs.pisugar.com/docs/product-wiki/whisplay/intro), and it drives a
separate Raspberry Pi Pico W robot car over WiFi.

```
  [You speak]
       |
       v
+-----------------------+        raw TCP socket        +----------------------+
| Raspberry Pi Zero 2 W |  ------- port 8765 -------->  | Raspberry Pi Pico W  |
| + WhisPlay HAT        |     "forward" / "left" /      | + motor driver       |
| voice_car_control_    |     "right" / "reverse" /     | + OLED (IP/battery)  |
| groq.py                |     "stop" / "speed:N"        |                      |
+-----------------------+                                +----------------------+
       |
       v
  Groq Whisper API
  (speech-to-text)
```

## What it does

1. **Hold the WhisPlay HAT's button** and speak a command.
2. On release, the recording is sent to **Groq's Whisper API** (`whisper-large-v3-turbo`) for
   speech-to-text.
3. The recognized text is matched against a small command vocabulary (exact match first, then a
   conservative fuzzy-match fallback for near-misses like "foward" -> forward).
4. The matched command is sent over a **raw TCP socket** (no HTTP/WebSocket - see
   [`send_command.py`](send_command.py) and the companion Pico firmware) to the robot car.
5. Movement commands auto-stop after a fixed hold time instead of running until a separate "stop"
   command: **2.0s for forward/reverse**, **0.3s for left/right** (calibrated against this car's
   speed/wheelbase to land at roughly a 90 degree turn - re-tune if your hardware differs).
6. Status (what was heard, what was sent, connection errors) is shown on the HAT's LCD and via its
   RGB LED (blue = idle, red = listening, yellow = transcribing, green = sent, orange = error).

## Repo contents

| File | Purpose |
|---|---|
| `voice_car_control_groq.py` | Main app: button -> record -> Groq STT -> match -> send to Pico |
| `whisplay.py` | Driver for the WhisPlay HAT (LCD/SPI, RGB LED, button, backlight) |
| `send_command.py` | One-shot CLI: send a single command to the Pico without the HAT/mic at all - used to relay commands typed/spoken to an LLM assistant instead of the physical mic |
| `.gitignore` | Excludes the local Vosk model directory, recordings, `__pycache__`, etc. |

The Pico-side firmware (WiFi connect, OLED display, motor control, and the port-8765 TCP command
server) is a separate MicroPython script that runs on the Pico W itself and isn't part of this repo.

## Hardware

- Raspberry Pi Zero 2 W + PiSugar WhisPlay HAT (1.69" SPI LCD, dual mic, speaker, RGB LED, button)
- Raspberry Pi Pico W + a 4-motor robot car chassis + SSD1306 OLED
- Both devices on the same WiFi network (they talk to each other by local IP, not through the
  internet)

## Setup (Pi Zero side)

1. Install the WhisPlay HAT's audio/SPI driver (unified sound card + `raspi-config` SPI enable) -
   see [PiSugar/Whisplay](https://github.com/PiSugar/Whisplay).
2. `pip install groq websocket-client Pillow --break-system-packages` (plus whatever the driver
   install already pulled in: `spidev`, `gpiod`).
3. `sox` and `amixer`/`alsa-utils` for audio normalization and mic gain (`sudo apt install sox`).
4. Get a free Groq API key at [console.groq.com/keys](https://console.groq.com/keys) - the free
   tier is rate-limited, not billed, unless you add a payment method.
5. Set environment variables (or bake them into a `run.sh`):

   ```bash
   export GROQ_API_KEY=gsk_...
   export PICO_HOST=192.168.x.x     # IP shown on the Pico's OLED after it connects to WiFi
   export PICO_PORT=8765            # must match the Pico firmware's listen port
   export MIC_GAIN=100              # see note below
   export MOVE_DURATION_SEC=2.0     # forward/reverse hold time
   export TURN_DURATION_SEC=0.3     # left/right hold time - re-calibrate per car
   ```

   **Mic gain matters a lot on this HAT**: at the driver's default (80/100), the recorded signal
   only used ~5% of the available dynamic range, which noticeably hurt transcription accuracy.
   `MIC_GAIN=100` (near-max) fixed it - if you're getting poor recognition, check this first.

6. Run it:

   ```bash
   python3 voice_car_control_groq.py
   ```

   Or run it as a systemd service so it starts on boot and restarts on failure - see the service
   unit used in this project (`voice-car-control.service`, `ExecStart=... run.sh`,
   `Restart=on-failure`).

## Controlling it without the mic

If the HAT's mic is inconvenient to talk into, `send_command.py` sends a single command directly:

```bash
python3 send_command.py forward
python3 send_command.py right 0.5   # override the hold duration, e.g. for turn calibration
python3 send_command.py stop
```

This is also how a chat assistant can relay spoken/typed commands over SSH without touching the
HAT's mic pipeline at all.

## Command vocabulary

| You say | Sent to Pico |
|---|---|
| forward, ahead, go | `forward` |
| backward, reverse, back | `reverse` |
| left | `left` |
| right | `right` |
| stop, halt | `stop` |

Fuzzy matching only kicks in on short utterances (<=3 words) and is intentionally asymmetric: a
generous cutoff for "stop" (a false trigger just halts the car - harmless), a strict cutoff for
movement words (a false trigger could drive it into something).
