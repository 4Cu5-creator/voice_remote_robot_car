#!/usr/bin/env python3
"""
Voice-controlled robot car front-end for the PiSugar WhisPlay HAT.

Same hold-button-to-talk flow as the original project, but speech-to-text
runs on Groq's cloud Whisper API (console.groq.com) instead of the local
Vosk model:

  hold button -> record from the HAT mic
  release button -> Groq Whisper transcription -> show recognized text on
                    the LCD -> send the matching command over a raw TCP
                    socket to the Raspberry Pi Pico W robot car.

Requires GROQ_API_KEY to be set in the environment. Get a free key at
https://console.groq.com/keys - the free tier has no charge, just rate
limits (requests/tokens per minute/day), so this costs nothing unless you
add billing to the account.
"""

import difflib
import json
import os
import subprocess
import sys
import threading
import time

import socket as pysocket

from PIL import Image, ImageDraw, ImageFont
from groq import Groq

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.append(SCRIPT_DIR)

from whisplay import WhisplayBoard  # noqa: E402

# ==================== Configuration ====================
# IMPORTANT: set this to the IP address shown on the Pico's OLED screen
# after it connects to WiFi (or export PICO_HOST before running).
PICO_HOST = os.environ.get("PICO_HOST", "192.168.x.x")
PICO_PORT = int(os.environ.get("PICO_PORT", "8765"))

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
# Fastest / cheapest Whisper model on Groq - free tier is rate-limited, not
# charged, so this is the right default for "make it free".
GROQ_MODEL = os.environ.get("GROQ_MODEL", "whisper-large-v3-turbo")
# Nudges the model toward our command vocabulary without hard-locking it
# (Groq's API doesn't support Vosk-style grammar constraints).
GROQ_PROMPT = "forward reverse left right stop backward back halt ahead go"

AUDIO_DEVICE = "whisplaysound"
SAMPLE_RATE = 16000
RECORD_PATH = "/tmp/voice_cmd.wav"
NORMALIZED_PATH = "/tmp/voice_cmd_norm.wav"
MAX_RECORD_SEC = 8
# The WhisPlay mic needs to be near max gain to get a usable signal level
# (measured: mic=80 -> peak ~5% of full scale, mic=100 -> peak ~88%).
MIC_GAIN = int(os.environ.get("MIC_GAIN", "100"))

# Movement commands auto-stop after a set hold time so "forward" etc. is
# a bounded nudge instead of driving until a separate "stop" command.
# turn_left()/turn_right() on the Pico are open-loop (just spin one
# motor until stop()), so "90 degrees" is purely a function of how long
# we hold the turn - TURN_DURATION_SEC is a starting guess and needs to
# be calibrated against the real car (see send_command.py's duration
# override for quick iteration: `send_command.py right 0.5`).
MOVE_DURATION_SEC = float(os.environ.get("MOVE_DURATION_SEC", "2.0"))  # forward/reverse
TURN_DURATION_SEC = float(os.environ.get("TURN_DURATION_SEC", "0.3"))  # left/right, calibrated ~90 deg
STRAIGHT_COMMANDS = {"forward", "reverse"}
TURN_COMMANDS = {"left", "right"}
MOVE_COMMANDS = STRAIGHT_COMMANDS | TURN_COMMANDS


def move_hold_seconds(command):
    return TURN_DURATION_SEC if command in TURN_COMMANDS else MOVE_DURATION_SEC

# Groq/Whisper is open-vocabulary - the prompt only biases it, it can
# still transcribe to any word. If no word in the utterance exactly
# matches our vocabulary, fall back to the closest match by character
# similarity (catches things like "lift"->left, "foward"->forward).
#
# This is asymmetric on purpose: a false-positive "stop" is harmless
# (the car just halts), but a false-positive "left"/"right"/"forward"
# could send it into something. Testing against adversarial input showed
# short target words collide badly - "light" and "shop" both land in the
# same ~0.75-0.85 similarity band as genuine near-misses like "lift"/
# "left", so no single cutoff cleanly separates them. So: movement
# commands require a strict cutoff, "stop" gets a forgiving one. Longer
# unrelated sentences ("what time is it") are rejected outright rather
# than scanned word-by-word.
STOP_FUZZY_CUTOFF = float(os.environ.get("STOP_FUZZY_CUTOFF", "0.7"))
MOVE_FUZZY_CUTOFF = float(os.environ.get("MOVE_FUZZY_CUTOFF", "0.82"))
MAX_FUZZY_WORDS = int(os.environ.get("MAX_FUZZY_WORDS", "3"))

# Recognized words -> canonical command sent to the Pico (must match the
# car_commands dict keys in the Pico's Microdot app: forward / reverse /
# left / right / stop).
SYNONYM_MAP = {
    "forward": "forward",
    "ahead": "forward",
    "go": "forward",
    "backward": "reverse",
    "reverse": "reverse",
    "back": "reverse",
    "left": "left",
    "right": "right",
    "stop": "stop",
    "halt": "stop",
}

# ==================== Display helpers ====================
LCD_W, LCD_H = WhisplayBoard.LCD_WIDTH, WhisplayBoard.LCD_HEIGHT


def _load_font(size, bold=False):
    candidates = []
    if bold:
        candidates.append("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
    candidates.append("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


TITLE_FONT = _load_font(22, bold=True)
BODY_FONT = _load_font(18)
SMALL_FONT = _load_font(14)


def rgb565_bytes(image: Image.Image) -> bytes:
    rgb = image.convert("RGB")
    out = bytearray()
    for y in range(rgb.height):
        for x in range(rgb.width):
            r, g, b = rgb.getpixel((x, y))
            value = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            out.append((value >> 8) & 0xFF)
            out.append(value & 0xFF)
    return bytes(out)


def wrap_text(draw, text, font, max_width):
    words = text.split()
    if not words:
        return [""]
    lines, current = [], words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if (bbox[2] - bbox[0]) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def render_panel(title, lines, accent=(60, 150, 255), footer="", background=(11, 16, 24)):
    image = Image.new("RGB", (LCD_W, LCD_H), background)
    draw = ImageDraw.Draw(image)
    content_width = LCD_W - 48

    draw.rounded_rectangle((12, 16, LCD_W - 12, LCD_H - 16), radius=18,
                            fill=(20, 28, 40), outline=(40, 55, 72), width=2)
    draw.rounded_rectangle((20, 22, LCD_W - 20, 58), radius=12, fill=accent)
    draw.text((30, 31), title, fill=(255, 255, 255), font=SMALL_FONT)

    y = 74
    for line in lines:
        for wrapped in wrap_text(draw, line, BODY_FONT, content_width):
            draw.text((24, y), wrapped, fill=(214, 225, 236), font=BODY_FONT)
            y += 26
        if line == "":
            y += 10

    if footer:
        footer_top = LCD_H - 50
        draw.rounded_rectangle((20, footer_top, LCD_W - 20, LCD_H - 22), radius=12,
                                fill=(28, 37, 50))
        draw.text((28, footer_top + 8), footer, fill=(150, 205, 255), font=SMALL_FONT)

    return rgb565_bytes(image)


# ==================== TCP socket client to the Pico ====================
# The Pico runs a raw line-delimited TCP server (no HTTP/WebSocket handshake)
# on PICO_PORT: send "<command>\n", read back "<reply>\n".
class PicoLink:
    def __init__(self, host, port, timeout=4):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock = None
        self._lock = threading.Lock()

    def _connect(self):
        self._sock = pysocket.create_connection((self.host, self.port), timeout=self.timeout)

    def send_command(self, command):
        """Send a plain command string, return (ok, reply_or_error)."""
        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._sock is None:
                        self._connect()
                    self._sock.sendall((command + "\n").encode())
                    self._sock.settimeout(self.timeout)
                    reply = self._sock.recv(256).decode().strip()
                    return True, reply
                except Exception as exc:
                    if self._sock is not None:
                        try:
                            self._sock.close()
                        except Exception:
                            pass
                    self._sock = None
                    if attempt == 2:
                        return False, str(exc)

    def close(self):
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                except Exception:
                    pass
                self._sock = None


# ==================== Speech-to-text (Groq cloud Whisper) ====================
class GroqTranscriber:
    def __init__(self, api_key, model):
        if not api_key:
            raise RuntimeError(
                "GROQ_API_KEY is not set. Get a free key at "
                "https://console.groq.com/keys and export it before running."
            )
        self.client = Groq(api_key=api_key)
        self.model = model

    def transcribe(self, wav_path):
        """Returns (text_or_None, error_or_None)."""
        try:
            with open(wav_path, "rb") as f:
                result = self.client.audio.transcriptions.create(
                    file=(os.path.basename(wav_path), f.read()),
                    model=self.model,
                    language="en",
                    prompt=GROQ_PROMPT,
                    response_format="json",
                    temperature=0.0,
                )
            return result.text.strip(), None
        except Exception as exc:
            return None, str(exc)


def set_mic_gain(gain):
    try:
        subprocess.run(
            ["amixer", "-c", AUDIO_DEVICE, "cset", "name=mic", str(gain)],
            check=False, capture_output=True, timeout=5,
        )
    except Exception as exc:
        print(f"Could not set mic gain: {exc}")


def normalize_audio(src_path, dst_path):
    """Digitally normalize the recording toward -0.5 dBFS peak so quiet
    input still gives the STT engine a healthy signal level, independent
    of the HAT's analog mic gain. Falls back to the raw recording if sox
    fails for any reason."""
    try:
        subprocess.run(
            ["sox", src_path, dst_path, "norm", "-0.5"],
            check=True, capture_output=True, timeout=10,
        )
        return dst_path
    except Exception as exc:
        print(f"Normalization failed, using raw recording: {exc}")
        return src_path


STOP_SYNONYMS = [w for w, cmd in SYNONYM_MAP.items() if cmd == "stop"]
MOVE_SYNONYMS = [w for w, cmd in SYNONYM_MAP.items() if cmd != "stop"]


def _best_fuzzy(word, vocab, cutoff):
    match = difflib.get_close_matches(word, vocab, n=1, cutoff=cutoff)
    if not match:
        return None, 0.0
    return match[0], difflib.SequenceMatcher(None, word, match[0]).ratio()


def extract_command(text):
    """Returns (command, matched_word, method) where method is
    "exact", "fuzzy", or None if nothing matched well enough."""
    words = [w.strip(".,!?") for w in text.lower().split()]

    # 1. Exact match against the vocabulary - fast and unambiguous.
    for word in words:
        if word in SYNONYM_MAP:
            return SYNONYM_MAP[word], word, "exact"

    # 2. Fuzzy fallback, gated to short utterances only - a long
    # unrelated sentence has too many words to safely scan one-by-one.
    if not words or len(words) > MAX_FUZZY_WORDS:
        return None, None, None

    best_word, best_score = None, 0.0
    for word in words:
        stop_word, stop_score = _best_fuzzy(word, STOP_SYNONYMS, STOP_FUZZY_CUTOFF)
        if stop_score > best_score:
            best_word, best_score = stop_word, stop_score
        move_word, move_score = _best_fuzzy(word, MOVE_SYNONYMS, MOVE_FUZZY_CUTOFF)
        if move_score > best_score:
            best_word, best_score = move_word, move_score

    if best_word is not None:
        return SYNONYM_MAP[best_word], best_word, "fuzzy"

    return None, None, None


# ==================== Main application ====================
class VoiceCarApp:
    def __init__(self):
        set_mic_gain(MIC_GAIN)
        self.board = WhisplayBoard()
        self.board.set_backlight(70)
        self.pico = PicoLink(PICO_HOST, PICO_PORT)
        self.transcriber = GroqTranscriber(GROQ_API_KEY, GROQ_MODEL)

        self._record_proc = None
        self._recording = False
        self._busy = False
        self._auto_stop_timer = None

        self.board.on_button_press(self._on_press)
        self.board.on_button_release(self._on_release)

        self.show_idle()

    # ---- display states ----
    def show_idle(self, note=""):
        self.board.set_rgb(0, 60, 160)
        frame = render_panel(
            "Voice Car Control (Groq)",
            ["Hold the button and speak", "a command:", "",
             "forward / reverse", "left / right / stop", note],
            accent=(60, 150, 255),
            footer=f"Pico: {PICO_HOST}:{PICO_PORT}",
        )
        self.board.draw_image(0, 0, LCD_W, LCD_H, frame)

    def show_listening(self):
        self.board.set_rgb(255, 0, 0)
        frame = render_panel(
            "Listening...",
            ["Speak now.", "Release the button", "when you're done."],
            accent=(220, 58, 58),
            footer="Recording",
        )
        self.board.draw_image(0, 0, LCD_W, LCD_H, frame)

    def show_thinking(self):
        self.board.set_rgb(255, 180, 0)
        frame = render_panel(
            "Thinking...",
            ["Sending to Groq for", "transcription..."],
            accent=(255, 180, 0),
            footer="Please wait",
        )
        self.board.draw_image(0, 0, LCD_W, LCD_H, frame)

    def show_result(self, heard_text, command, sent_ok, detail="", match_method=None):
        heard_display = heard_text if heard_text else "(nothing recognized)"
        if command and sent_ok:
            self.board.set_rgb(0, 255, 0)
            if command in MOVE_COMMANDS:
                hold = move_hold_seconds(command)
                sent_label = f"Sent: {command} ({hold:.1f}s)"
                self._flash_then_idle(delay=hold + 0.5)
            else:
                sent_label = f"Sent: {command}"
                self._flash_then_idle()
            lines = [f'Heard: "{heard_display}"', sent_label]
            if match_method == "fuzzy":
                lines.append("(fuzzy match)")
            frame = render_panel(
                "Command Sent",
                lines,
                accent=(0, 190, 90),
                footer="OK",
            )
        elif command and not sent_ok:
            self.board.set_rgb(255, 120, 0)
            frame = render_panel(
                "Send Failed",
                [f'Heard: "{heard_display}"', f"Command: {command}", detail[:60]],
                accent=(255, 130, 0),
                footer="Check Pico connection",
            )
        else:
            self.board.set_rgb(255, 120, 0)
            frame = render_panel(
                "Not Understood",
                [f'Heard: "{heard_display}"', "Try again with:",
                 "forward / reverse / left / right / stop"],
                accent=(255, 130, 0),
                footer="No command sent",
            )
        self.board.draw_image(0, 0, LCD_W, LCD_H, frame)

    def show_transcribe_error(self, detail):
        self.board.set_rgb(255, 0, 0)
        frame = render_panel(
            "Transcription Failed",
            ["Could not reach Groq API.", detail[:80]],
            accent=(220, 58, 58),
            footer="Check internet / GROQ_API_KEY",
        )
        self.board.draw_image(0, 0, LCD_W, LCD_H, frame)

    def _flash_then_idle(self, delay=1.5):
        def worker():
            time.sleep(delay)
            if not self._busy:
                self.show_idle()
        threading.Thread(target=worker, daemon=True).start()

    # ---- movement auto-stop ----
    def _cancel_auto_stop(self):
        if self._auto_stop_timer is not None:
            self._auto_stop_timer.cancel()
            self._auto_stop_timer = None

    def _schedule_auto_stop(self, delay):
        self._cancel_auto_stop()

        def do_stop():
            self.pico.send_command("stop")

        timer = threading.Timer(delay, do_stop)
        timer.daemon = True
        self._auto_stop_timer = timer
        timer.start()

    # ---- button handlers ----
    def _on_press(self):
        if self._busy:
            return
        self._busy = True
        self._recording = True
        self.show_listening()
        self._record_proc = subprocess.Popen(
            ["arecord", "-D", AUDIO_DEVICE, "-f", "S16_LE", "-r", str(SAMPLE_RATE),
             "-c", "1", "-t", "wav", "-d", str(MAX_RECORD_SEC), RECORD_PATH],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )

    def _on_release(self):
        if not self._recording:
            return
        self._recording = False
        proc = self._record_proc
        if proc is not None and proc.poll() is None:
            proc.send_signal(2)  # SIGINT: let arecord finalize the WAV header
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
        threading.Thread(target=self._process_recording, daemon=True).start()

    def _process_recording(self):
        try:
            self.show_thinking()
            if not os.path.exists(RECORD_PATH) or os.path.getsize(RECORD_PATH) <= 256:
                self.show_result("", None, False)
                return

            audio_path = normalize_audio(RECORD_PATH, NORMALIZED_PATH)
            text, error = self.transcriber.transcribe(audio_path)
            if error is not None:
                self.show_transcribe_error(error)
                return

            command, matched_word, match_method = extract_command(text)

            if command is None:
                self.show_result(text, None, False)
                return
            print(f'Heard "{text}" -> matched "{matched_word}" ({match_method}) -> {command}')

            # Cancel any pending auto-stop from a previous move before
            # dispatching this one, so it can't cut the new move short.
            self._cancel_auto_stop()
            ok, detail = self.pico.send_command(command)
            if ok and command in MOVE_COMMANDS:
                self._schedule_auto_stop(move_hold_seconds(command))
            self.show_result(text, command, ok, detail if not ok else "", match_method)
        finally:
            self._busy = False

    # ---- lifecycle ----
    def run(self):
        print(f"Voice car control (Groq) ready. Pico target: {PICO_HOST}:{PICO_PORT}")
        print("Hold the WhisPlay button and speak a command.")
        try:
            while True:
                time.sleep(0.2)
        except KeyboardInterrupt:
            pass
        finally:
            self._cancel_auto_stop()
            self.pico.send_command("stop")
            self.pico.close()
            self.board.set_rgb(0, 0, 0)
            self.board.cleanup()


if __name__ == "__main__":
    if not GROQ_API_KEY:
        print("ERROR: GROQ_API_KEY is not set.")
        print("Get a free key at https://console.groq.com/keys and run:")
        print("  export GROQ_API_KEY=gsk_...")
        sys.exit(1)
    VoiceCarApp().run()
