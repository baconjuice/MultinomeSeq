#!/usr/bin/env python3
# Monome Dual-Grid Sequencer with Tkinter GUI and MIDI out

import asyncio, functools, contextlib
import tkinter as tk
import monome, rtmidi

# ---------- Constants ----------
ROWS, COLS = 8, 16         # Grid size (rows × columns)
CELL_SIZE  = 20            # GUI cell size in pixels
BASE_NOTE  = 36            # MIDI base note for bottom-left grid cell (C2)
GATE_RATIO = 0.9           # Duration of each MIDI note (as a fraction of a clock step)

# ---------- Global Sequencer State ----------
class SeqState:
    def __init__(self):
        self.steps      = [[0] * COLS for _ in range(ROWS)]
        self.playcol    = 0
        self.running    = True
        self.bpm        = 120
        self.transpose  = 0

state = SeqState()

# ---------- Backend (Monome + MIDI) ----------
class Backend:
    def __init__(self):
        self.grids: list[monome.Grid] = []
        self.offsets: dict[str, int] = {}
        self.gui = None

        # MIDI setup
        self.midi_out = rtmidi.MidiOut()
        self.midi_port = None
        self.midi_chan = 0  # MIDI channel 0 = channel 1

        ports = self.midi_out.get_ports()
        if ports:
            self.midi_out.open_port(0)
            self.midi_port = ports[0]
        else:
            self.midi_out.open_virtual_port("MonomeSeq (virtual)")
            self.midi_port = "MonomeSeq (virtual)"

    def list_ports(self):
        return self.midi_out.get_ports()

    def set_port(self, name: str):
        if name == self.midi_port:
            return

        if self.midi_out.is_port_open():
            with contextlib.suppress(rtmidi.InvalidUseError):
                self.midi_out.close_port()

        for i, port in enumerate(self.list_ports()):
            if port == name:
                self.midi_out.open_port(i)
                self.midi_port = name
                return

        self.midi_out.open_virtual_port("MonomeSeq (virtual)")
        self.midi_port = "MonomeSeq (virtual)"

    async def start(self):
        asyncio.create_task(self.serialosc_connect())
        asyncio.create_task(self._clock_loop())

    async def serialosc_connect(self):
        self.sosc = monome.SerialOsc()
        self.sosc.device_added_event.add_handler(self._device_added)
        await self.sosc.connect()

    def _device_added(self, id_, typ, port):
        g = monome.Grid()
        asyncio.create_task(self._setup_grid(g, port))

    async def _setup_grid(self, grid: monome.Grid, port):
        await grid.connect("127.0.0.1", port)
        while grid.id is None:
            await asyncio.sleep(0.01)
        self.grids.append(grid)
        self.offsets[grid.id] = 0 if len(self.grids) == 1 else 8
        grid.key_event.add_handler(functools.partial(self._on_key, grid))
        self._redraw()

    def _on_key(self, grid, x, y, s):
        if s == 0:
            return
        vx = x + self.offsets[grid.id]
        state.steps[y][vx] ^= 1
        if self.gui and self.gui.canvas.winfo_exists():
            self.gui.draw_grid()
        self._redraw()

    def _redraw(self):
        for grid in self.grids:
            off = self.offsets[grid.id]
            for y in range(ROWS):
                grid.led_row(0, y, [state.steps[y][x] for x in range(off, off + 8)])
            if off <= state.playcol < off + 8:
                lx = state.playcol - off
                for y in range(ROWS):
                    grid.led_set(lx, y, 1)

    async def _clock_loop(self):
        while True:
            if state.running:
                notes = []
                for row in range(ROWS):
                    if state.steps[row][state.playcol]:
                        note = BASE_NOTE + state.transpose + (ROWS - 1 - row)
                        self.midi_out.send_message([0x90 | self.midi_chan, note, 100])
                        notes.append(note)
                self._redraw()
                try:
                    if self.gui and self.gui.canvas.winfo_exists():
                        self.gui.draw_grid()
                except tk.TclError:
                    break
                asyncio.create_task(self._note_off_after(notes))
                state.playcol = (state.playcol + 1) % COLS
            await asyncio.sleep(60 / state.bpm / 4)

    async def _note_off_after(self, notes):
        await asyncio.sleep((60 / state.bpm / 4) * GATE_RATIO)
        for n in notes:
            self.midi_out.send_message([0x80 | self.midi_chan, n, 0])

    def shutdown(self):
        if self.midi_out.is_port_open():
            with contextlib.suppress(rtmidi.InvalidUseError):
                self.midi_out.close_port()
        for g in self.grids:
            with contextlib.suppress(Exception):
                g.disconnect()

# ---------- GUI ----------
class SequencerGUI:
    def __init__(self, root: tk.Tk, backend: Backend):
        self.backend = backend
        backend.gui = self

        root.title("Monome Sequencer")
        self.root = root

        self.canvas = tk.Canvas(root, width=COLS * CELL_SIZE, height=ROWS * CELL_SIZE, bg="black")
        self.canvas.pack()
        self.canvas.bind("<Button-1>", self.on_canvas_click)

        ctrl = tk.Frame(root)
        ctrl.pack(pady=6)

        self.bpm_slider = tk.Scale(ctrl, from_=20, to=300, orient="horizontal", label="BPM",
                                   command=self.on_bpm)
        self.bpm_slider.set(state.bpm)
        self.bpm_slider.grid(row=0, column=0)

        self.play_btn = tk.Button(ctrl, text="Stop", width=6, command=self.toggle_play)
        self.play_btn.grid(row=0, column=1, padx=6)

        self.oct_label = tk.Label(ctrl, text="Octave +0")
        self.oct_label.grid(row=0, column=2)
        tk.Button(ctrl, text="▲", width=2, command=lambda: self.shift_oct(12)).grid(row=0, column=3)
        tk.Button(ctrl, text="▼", width=2, command=lambda: self.shift_oct(-12)).grid(row=0, column=4)

        tk.Label(ctrl, text="MIDI Port").grid(row=1, column=0)
        self.mvar = tk.StringVar(value=self.backend.midi_port)
        ports = self.backend.list_ports()
        initial_port = self.backend.midi_port or (ports[0] if ports else "None")
        self.mvar.set(initial_port)
        self.mmenu = tk.OptionMenu(ctrl, self.mvar, initial_port, *ports, command=self._set_port)
        self.mmenu.grid(row=1, column=1, columnspan=2, sticky="ew")
        tk.Button(ctrl, text="Refresh", command=self._refresh_ports).grid(row=1, column=3)

        tk.Label(ctrl, text="Ch").grid(row=1, column=4)
        self.ch_spin = tk.Spinbox(ctrl, from_=1, to=16, width=3, command=self._set_channel)
        self.ch_spin.grid(row=1, column=5)
        self.ch_spin.delete(0, "end")
        self.ch_spin.insert(0, str(self.backend.midi_chan + 1))

        self.draw_grid()

    def draw_grid(self):
        self.canvas.delete("all")
        for y in range(ROWS):
            for x in range(COLS):
                row_disp = ROWS - 1 - y
                if x == state.playcol:
                    fill = "lime green" if state.steps[y][x] else "dark green"
                else:
                    fill = "white" if state.steps[y][x] else "gray20"
                self.canvas.create_rectangle(
                    x * CELL_SIZE, row_disp * CELL_SIZE,
                    (x + 1) * CELL_SIZE, (row_disp + 1) * CELL_SIZE,
                    fill=fill, outline="gray50"
                )

    def on_canvas_click(self, ev):
        col = ev.x // CELL_SIZE
        row_disp = ev.y // CELL_SIZE
        y = ROWS - 1 - row_disp
        if 0 <= col < COLS and 0 <= y < ROWS:
            state.steps[y][col] ^= 1
            self.draw_grid()
            self.backend._redraw()

    def on_bpm(self, val):
        state.bpm = int(float(val))

    def toggle_play(self):
        state.running = not state.running
        self.play_btn.config(text="Stop" if state.running else "Play")

    def shift_oct(self, semis):
        state.transpose += semis
        self.oct_label.config(text=f"Octave {state.transpose // 12:+d}")

    def _set_port(self, name):
        self.backend.set_port(name)
        self.mvar.set(self.backend.midi_port)

    def _refresh_ports(self):
        new_ports = self.backend.list_ports()
        menu = self.mmenu["menu"]
        menu.delete(0, "end")
        for port in new_ports:
            menu.add_command(label=port, command=lambda p=port: self._set_port(p))
        if self.mvar.get() not in new_ports and new_ports:
            self._set_port(new_ports[0])
        self.mvar.set(self.backend.midi_port)

    def _set_channel(self):
        try:
            ch = max(1, min(16, int(self.ch_spin.get())))
            self.backend.midi_chan = ch - 1
        except ValueError:
            pass

# ---------- Main Loop ----------
async def main():
    backend = Backend()
    root = tk.Tk()
    gui = SequencerGUI(root, backend)
    await backend.start()

    try:
        while True:
            try:
                if not root.winfo_exists():
                    break
                root.update()
            except tk.TclError:
                break
            await asyncio.sleep(0.01)
    except KeyboardInterrupt:
        print("Ctrl+C — exiting.")
    finally:
        backend.shutdown()
        try:
            root.destroy()
        except tk.TclError:
            pass

if __name__ == "__main__":
    asyncio.run(main())
