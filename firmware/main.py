"""MicroPython firmware for the three-colour ESP32-C3 status light.

Protocol:
    host -> board: STATE:<name>\n
    board -> host: OK:<name>\n

The retail unit used for this project has counter-intuitive wire colours.  The
GPIO mapping below is the tested mapping for that unit, independent of the
colours printed on its wires.
"""

from machine import Pin, PWM
import select
import sys
import time


RED = PWM(Pin(3, Pin.OUT), freq=1000, duty=0)
YELLOW = PWM(Pin(4, Pin.OUT), freq=1000, duty=0)
GREEN = PWM(Pin(5, Pin.OUT), freq=1000, duty=0)

pulse = 0
state = "OFF"


def leds(red, yellow, green):
    RED.duty(red)
    YELLOW.duty(yellow)
    GREEN.duty(green)


def fx_idle():
    global pulse
    phase = pulse % 200
    brightness = phase * 10 if phase < 100 else (200 - phase) * 10
    leds(0, 0, brightness)


def fx_thinking():
    leds(0, 1023, 0) if (pulse % 60) < 20 else leds(0, 0, 0)


def fx_busy():
    leds(0, 1023, 0) if (pulse % 80) < 40 else leds(0, 0, 1023)


def fx_success():
    leds(0, 0, 1023)


def fx_error():
    leds(1023, 0, 0) if (pulse % 30) < 15 else leds(0, 0, 0)


def fx_confirm():
    leds(0, 1023, 0)


def fx_off():
    # OFF means OpenClaw/Gateway unavailable, not electrical power-off.
    leds(1023, 0, 0)


EFFECTS = {
    "IDLE": fx_idle,
    "THINKING": fx_thinking,
    "BUSY": fx_busy,
    "SUCCESS": fx_success,
    "DONE": fx_success,
    "ERROR": fx_error,
    "WAIT_CONFIRM": fx_confirm,
    "OFF": fx_off,
}


print("READY")
buffer = ""
poller = select.poll()
poller.register(sys.stdin, select.POLLIN)

while True:
    if poller.poll(0):
        char = sys.stdin.read(1)
        if char == "\n":
            command = buffer.strip()
            buffer = ""
            if command == "PING":
                print("PONG")
            elif command.startswith("STATE:"):
                requested = command[6:].strip().upper()
                if requested in EFFECTS:
                    state = requested
                    print("OK:" + state)
                else:
                    print("ERR:Unknown")
            elif command:
                print("ERR:Unknown")
        elif char:
            buffer += char

    EFFECTS.get(state, fx_off)()
    pulse = (pulse + 1) % 10000
    time.sleep_ms(10)
