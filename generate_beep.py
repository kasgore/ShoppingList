"""Generate static/beep.wav — a classic kitchen-timer bell ring.

Produces 4 successive bell strikes with harmonic overtones (fundamental,
2.001x, 3.005x, 4.91x) and exponential decay so it sounds like a bell or
a chime rather than a buzzer. Run once: `python generate_beep.py`."""
from __future__ import annotations

import math
import os
import struct
import wave

OUT = os.path.join(os.path.dirname(__file__), "static", "beep.wav")
SR = 44100
F0 = 1000.0           # fundamental — bright but not piercing
RING_DUR = 0.55       # length of each strike's audible decay
GAP = 0.18            # silence between strikes
NUM_RINGS = 4
TOTAL = NUM_RINGS * (RING_DUR + GAP)


def strike(t: float) -> float:
    """One bell strike at time t (seconds since the strike began)."""
    return (
        0.55 * math.sin(2 * math.pi * F0          * t) * math.exp(-t * 2.0) +
        0.30 * math.sin(2 * math.pi * F0 * 2.001  * t) * math.exp(-t * 3.4) +
        0.15 * math.sin(2 * math.pi * F0 * 3.005  * t) * math.exp(-t * 4.5) +
        0.08 * math.sin(2 * math.pi * F0 * 4.91   * t) * math.exp(-t * 6.0)
    )


def main() -> None:
    n = int(SR * TOTAL)
    starts = [k * (RING_DUR + GAP) for k in range(NUM_RINGS)]
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with wave.open(OUT, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SR)
        for i in range(n):
            t = i / SR
            s = 0.0
            for start in starts:
                if start <= t < start + RING_DUR:
                    s += strike(t - start)
            # Soft attack on each strike to avoid clicks.
            for start in starts:
                rel = t - start
                if 0 <= rel < 0.005:
                    s *= rel / 0.005
            # Soft clip to keep peaks under digital max.
            if s > 1: s = 1.0 - math.exp(-s + 1)
            if s < -1: s = -(1.0 - math.exp(s + 1))
            f.writeframesraw(struct.pack("<h", int(s * 32767 * 0.85)))
    print(f"wrote {OUT} ({os.path.getsize(OUT)} bytes)")


if __name__ == "__main__":
    main()
