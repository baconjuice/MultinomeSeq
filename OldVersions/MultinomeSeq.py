#!/usr/bin/env python3
# Requires:  pip install pymonome python-rtmidi

import asyncio, functools, signal
import monome                 # pymonome
import rtmidi                 # python-rtmidi

# ---------- user defaults ----------
BPM_DEFAULT   = 120
BASE_NOTE     = 36        # C2 on bottom row (y = 7)
MIDI_CHANNEL  = 0         # 0 = channel 1
GATE_RATIO    = 0.9       # note length = step * ratio
# ------------------------------------

class Sequencer:
    def __init__(self):
        # dual-grid bookkeeping
        self.grids: list[monome.Grid] = []
        self.offsets: dict[str, int] = {}     # grid.id → x-offset
        # sequence data  (8 rows × 16 cols)
        self.steps   = [[0]*16 for _ in range(8)]
        self.playcol = 0
        # tempo / pitch
        self.bpm       = BPM_DEFAULT
        self.transpose = 0                    # semitones
        # midi
        self.midi = rtmidi.MidiOut()
        ports = self.midi.get_ports()
        (self.midi.open_port if ports else self.midi.open_virtual_port)(
            0 if ports else "MonomeSeq"
        )
        print("MIDI port opened.")
        # tasks
        self.clock_task = None

    # ---------- serialosc / grid ----------
    async def start(self):
        self.sosc = monome.SerialOsc()
        self.sosc.device_added_event.add_handler(self._on_device_added)
        await self.sosc.connect()
        # clock
        self.clock_task = asyncio.create_task(self.run_clock())

    def _on_device_added(self, id_, type_, port):
        print(f"connecting to {id_} ({type_})")
        grid = monome.Grid()
        asyncio.create_task(self._setup_grid(grid, port))

    async def _setup_grid(self, grid: monome.Grid, port,):
        await grid.connect("127.0.0.1", port)
        while grid.id is None:                 # wait for metadata
            await asyncio.sleep(0.01)

        self.grids.append(grid)                # now len() is correct
        offset = 0 if len(self.grids) == 1 else 8
        self.offsets[grid.id] = offset

        grid.key_event.add_handler(functools.partial(self._on_key, grid))
        print(f"connected: {grid.id} offset {offset}")
        self.draw_static()

    # ---------- key handling ----------
    def _on_key(self, grid, x, y, s):
        if s == 0:                          # ignore key-up
            return
        vx = x + self.offsets[grid.id]

        # Right-hand control column (x == 15)
        if vx == 15:
            if y == 0:   self.transpose += 12
            elif y == 1: self.transpose -= 12
            elif y == 2: self.bpm = max(20, self.bpm + 5)
            elif y == 3: self.bpm = max(20, self.bpm - 5)
            print(f"BPM {self.bpm}  Transpose {self.transpose}")
            self.draw_static()
            return

        # Toggle step
        self.steps[y][vx] ^= 1
        self.draw_static()

    # ---------- drawing (binary LEDs) ----------
    def draw_static(self):
        for grid in self.grids:
            off = self.offsets[grid.id]
            for y in range(8):
                row = []
                for x in range(8):
                    gx = off + x
                    led = 1 if self.steps[y][gx] else 0
                    # keep control buttons lit
                    if gx == 15 and y in (0,1,2,3):
                        led = 1
                    row.append(led)
                grid.led_row(0, y, row)
        self.draw_playhead()                 # overlay playhead

    def draw_playhead(self):
        for grid in self.grids:
            off = self.offsets[grid.id]
            if off <= self.playcol < off+8:
                lx = self.playcol - off
                for y in range(8):
                    grid.led_set(lx, y, 1)  # always ON at playhead

    # ---------- clock & MIDI ----------
    async def run_clock(self):
        while True:
            step_len = 60 / self.bpm / 4        # 16th note
            notes_on = []
            for row in range(8):
                if self.steps[row][self.playcol]:
                    note = BASE_NOTE + self.transpose + (7 - row)
                    self.note_on(note)
                    notes_on.append(note)

            self.draw_static()                  # refresh LEDs

            # schedule note-offs
            asyncio.create_task(self._note_off_after(notes_on,
                                                     step_len * GATE_RATIO))

            await asyncio.sleep(step_len)
            self.playcol = (self.playcol + 1) % 16

    async def _note_off_after(self, notes, delay):
        await asyncio.sleep(delay)
        for n in notes:
            self.note_off(n)

    # ---------- MIDI helpers ----------
    def note_on(self, note, vel=100):
        self.midi.send_message([0x90 | MIDI_CHANNEL, note & 0x7F, vel & 0x7F])

    def note_off(self, note):
        self.midi.send_message([0x80 | MIDI_CHANNEL, note & 0x7F, 0])

    # ---------- shutdown ----------
    async def shutdown(self):
        if self.clock_task:
            self.clock_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.clock_task
        for g in self.grids:
            g.led_all(0)                      # clear LEDs
        self.midi.close_port()

    def __del__(self):
        # Fallback in case shutdown wasn’t awaited
        if self.midi:
            self.midi.close_port()

# ---------- main ----------
import contextlib

async def main():
    seq = Sequencer()
    await seq.start()

    loop = asyncio.get_running_loop()
    stop = loop.create_future()

    # Ctrl-C / SIGTERM => cancel future
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.cancel)

    try:
        await stop                       # run until signal
    except asyncio.CancelledError:
        pass
    finally:
        await seq.shutdown()
        print("Sequencer stopped cleanly.")

if __name__ == "__main__":
    asyncio.run(main())
