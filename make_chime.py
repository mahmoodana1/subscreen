#!/usr/bin/env python3
"""Generate the prayer chime as a WAV — no audio file or package needed.

Three ascending notes with an exponential decay, which reads as a
notification rather than an alarm. Run once; the result is copied to the
phone next to subscreen_tui.py.

    python3 make_chime.py [out.wav]
"""

import math
import struct
import sys
import wave

RATE = 44100
NOTES = [(659.25, 0.16), (783.99, 0.16), (1046.50, 0.42)]   # E5, G5, C6
AMPLITUDE = 0.32


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "chime.wav"
    frames = bytearray()

    for index, (freq, seconds) in enumerate(NOTES):
        count = int(RATE * seconds)
        for i in range(count):
            t = i / RATE
            # Exponential decay, plus a short fade-in so it never clicks.
            envelope = math.exp(-t * (3.0 if index < len(NOTES) - 1 else 4.5))
            envelope *= min(1.0, i / (RATE * 0.005))
            # A touch of second harmonic keeps it from sounding like a test tone.
            sample = math.sin(2 * math.pi * freq * t)
            sample += 0.25 * math.sin(4 * math.pi * freq * t)
            value = int(max(-1.0, min(1.0, sample / 1.25 * envelope * AMPLITUDE)) * 32767)
            frames += struct.pack("<h", value)

    with wave.open(path, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(bytes(frames))
    print(f"wrote {path} ({len(frames) // 2} frames)")


if __name__ == "__main__":
    main()
