#!/usr/bin/env python3
# integrated_seq.py
# Tkinter GUI  +  dual-Monome  +  MIDI  (binary LEDs)

import asyncio, functools, signal, contextlib
import tkinter as tk
import monome
import rtmidi

# ---------- constants ----------
ROWS, COLS   = 8, 16
CELL_SIZE    = 20            # GUI square size
BASE_NOTE    = 36            # C2 on bottom row
GATE_RATIO   = 0.9           # note length = step * ratio
MIDI_CH      = 0             # channel-1

# ---------- shared state ----------
class SeqState:
    def __init__(self):
        self.steps      = [[0]*COLS for _ in range(ROWS)]
        self.playcol    = 0
        self.running    = True
        self.bpm        = 120
        self.transpose  = 0

state = SeqState()            # global-ish singleton for brevity

# ---------- Tkinter GUI ----------
class SequencerGUI:
    def __init__(self, root):
        self.root = root
        root.title("Monome Sequencer")

        # canvas (grid)
        self.canvas = tk.Canvas(root, width=COLS*CELL_SIZE,
                                height=ROWS*CELL_SIZE, bg="black")
        self.canvas.pack()
        self.canvas.bind("<Button-1>", self.on_click)

        # control frame
        ctrl = tk.Frame(root); ctrl.pack(pady=6)

        self.bpm_slider = tk.Scale(ctrl, from_=20, to=300, orient="horizontal",
                                   label="BPM", command=self.on_bpm)
        self.bpm_slider.set(state.bpm); self.bpm_slider.grid(row=0, column=0)

        self.play_btn = tk.Button(ctrl, text="Stop", width=6,
                                  command=self.toggle_play)
        self.play_btn.grid(row=0, column=1, padx=6)

        self.oct_label = tk.Label(ctrl, text="Octave +0")
        self.oct_label.grid(row=0, column=2)

        tk.Button(ctrl, text="▲", width=2,
                  command=lambda: self.shift_oct(12)).grid(row=0, column=3)
        tk.Button(ctrl, text="▼", width=2,
                  command=lambda: self.shift_oct(-12)).grid(row=0, column=4)

        self.draw_grid()      # first paint

    # --- GUI helpers ---
    def draw_grid(self):
        self.canvas.delete("all")
        for y in range(ROWS):
            for x in range(COLS):
                row_disp = ROWS-1-y          # flip vertically
                if x == state.playcol:       # play-head column
                    fill = "lime green" if state.steps[y][x] else "dark green"
                else:
                    fill = "white" if state.steps[y][x] else "gray20"
                self.canvas.create_rectangle(
                    x*CELL_SIZE, row_disp*CELL_SIZE,
                    (x+1)*CELL_SIZE, (row_disp+1)*CELL_SIZE,
                    fill=fill, outline="gray50"
                )

    def on_click(self, ev):
        col = ev.x // CELL_SIZE
        row_disp = ev.y // CELL_SIZE
        y = ROWS-1-row_disp
        if 0<=col<COLS and 0<=y<ROWS:
            state.steps[y][col] ^= 1
            self.draw_grid()             # repaint
            backend.redraw_monome()      # sync LEDs

    def on_bpm(self, val):
        state.bpm = int(float(val))

    def toggle_play(self):
        state.running = not state.running
        self.play_btn.config(text="Stop" if state.running else "Play")

    def shift_oct(self, semis):
        state.transpose += semis
        self.oct_label.config(text=f"Octave {state.transpose//12:+d}")

# ---------- Monome + MIDI backend ----------
class Backend:
    def __init__(self):
        # monome
        self.grids   = []
        self.offsets = {}              # id → 0 or 8

        # midi
        self.midi = rtmidi.MidiOut()
        ports = self.midi.get_ports()
        (self.midi.open_port if ports else self.midi.open_virtual_port)(
            0 if ports else "MonomeSeq"
        )
        print("MIDI port open:", self.midi.get_ports()[0] if ports else "virtual")

        # start tasks
        asyncio.create_task(self.serialosc_connect())
        asyncio.create_task(self.clock_loop())

    # --- Serialosc discovery ---
    async def serialosc_connect(self):
        self.sosc = monome.SerialOsc()
        self.sosc.device_added_event.add_handler(self._device_added)
        await self.sosc.connect()

    def _device_added(self, id_, type_, port):
        print("connecting", id_, type_)
        g = monome.Grid()
        asyncio.create_task(self._setup_grid(g, port))

    async def _setup_grid(self, grid, port):
        await grid.connect("127.0.0.1", port)
        while grid.id is None:
            await asyncio.sleep(0.01)
        self.grids.append(grid)
        off = 0 if len(self.grids)==1 else 8
        self.offsets[grid.id] = off
        grid.key_event.add_handler(functools.partial(self._on_key, grid))
        print("connected:", grid.id, "offset", off)
        self.redraw_monome()

    # --- Monome key event ---
    def _on_key(self, grid, x, y, s):
        if not s: return              # ignore key-up
        vx = x + self.offsets[grid.id]
        state.steps[y][vx] ^= 1       # toggle
        gui.draw_grid()
        self.redraw_monome()

    # --- Draw LEDs (binary) ---
    def redraw_monome(self):
        for grid in self.grids:
            off = self.offsets[grid.id]
            for y in range(ROWS):
                slice8 = [state.steps[y][x] for x in range(off, off+8)]
                grid.led_row(0, y, slice8)
            # overlay play-head
            if off <= state.playcol < off+8:
                lx = state.playcol - off
                for y in range(ROWS):
                    grid.led_set(lx, y, 1)

    # --- Clock / MIDI ---
    async def clock_loop(self):
        while True:
            if state.running:
                # note-on for active steps
                notes = []
                for row in range(ROWS):
                    if state.steps[row][state.playcol]:
                        note = BASE_NOTE + state.transpose + (ROWS-1-row)
                        self.midi.send_message([0x90|MIDI_CH, note, 100])
                        notes.append(note)

                self.redraw_monome()     # LEDs (play-head already set)
                gui.draw_grid()          # GUI refresh

                # schedule note-offs
                asyncio.create_task(self.note_off_after(notes))

                state.playcol = (state.playcol+1) % COLS

            # wait one 16th note
            step_ms = 60/state.bpm/4 * 1000
            await asyncio.sleep(step_ms/1000)

    async def note_off_after(self, notes):
        await asyncio.sleep((60/state.bpm/4)*GATE_RATIO)
        for n in notes:
            self.midi.send_message([0x80|MIDI_CH, n, 0])

# ---------- cooperative Tk + asyncio loop ----------
def integrate_loops(root, loop):
    """Run asyncio tasks without blocking Tkinter."""
    loop.call_soon(loop.stop)
    loop.run_forever()
    root.after(10, integrate_loops, root, loop)

# ---------- start everything ----------
async def main_async():
    global backend
    backend = Backend()        # needs async tasks
    # no await here – backend manages its own tasks

def main():
    # Tk root & GUI
    root = tk.Tk()
    global gui
    gui = SequencerGUI(root)

    # asyncio loop
    loop = asyncio.get_event_loop()
    loop.create_task(main_async())

    # handle Ctrl-C cleanly
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, loop.stop)

    # start coop loop
    root.after(10, integrate_loops, root, loop)
    try:
        root.mainloop()
    finally:
        loop.stop()
        with contextlib.suppress(asyncio.CancelledError):
            loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()

if __name__ == "__main__":
    main()
