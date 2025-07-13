import asyncio
import contextlib
import functools
import tkinter as tk
import monome
import rtmidi

# ---------- constants ----------
ROWS, COLS = 8, 16         # Grid size (rows × columns)
CELL_SIZE  = 20            # GUI cell size in pixels
BASE_NOTE  = 36            # MIDI base note for bottom-left grid cell (row 7, column 0)
GATE_RATIO = 0.9           # Duration of each MIDI note (as a fraction of a clock step)

# ---------- global sequencer state ----------
class SeqState:
    def __init__(self):
        self.steps      = [[0] * COLS for _ in range(ROWS)]
        self.playcol    = 0
        self.running    = True
        self.bpm        = 120
        self.transpose  = 0

state = SeqState()

# ---------- backend (Monome + MIDI) ----------
class Backend:
    def __init__(self):
        self.grids, self.offsets = [], {}
        self.gui = None

        self.midi_out = rtmidi.MidiOut()
        ports = self.midi_out.get_ports()

        if ports:
            self.midi_out.open_port(0)
            self.midi_port = ports[0]
            self.midi_virtual = False
        else:
            self.midi_out.open_virtual_port("MonomeSeq (virtual)")
            self.midi_port = "MonomeSeq (virtual)"
            self.midi_virtual = True

        self.midi_chan = 0

    def list_ports(self):
        return self.midi_out.get_ports()

    def set_port(self, name: str):
        if name == self.midi_port:
            return  # Already selected

        # Fully reset the MIDI output device
        try:
            self.midi_out.close_port()
        except Exception:
            pass

        self.midi_out = rtmidi.MidiOut()  # ← Recreate MIDI device
        ports = self.midi_out.get_ports()

        for i, p in enumerate(ports):
            if p == name:
                try:
                    self.midi_out.open_port(i)
                    self.midi_port = name
                    self.midi_virtual = False
                    return
                except rtmidi.InvalidUseError as e:
                    print(f"Failed to open MIDI port '{name}': {e}")
                    break

        # Fallback to virtual port
        try:
            self.midi_out.open_virtual_port("MonomeSeq (virtual)")
            self.midi_port = "MonomeSeq (virtual)"
            self.midi_virtual = True
        except rtmidi.InvalidUseError as e:
            print(f"Failed to open virtual port: {e}")

    async def start(self):
        asyncio.create_task(self._connect_serialosc())
        asyncio.create_task(self._clock_loop())

    async def _connect_serialosc(self):
        self.sosc = monome.SerialOsc()
        self.sosc.device_added_event.add_handler(self._device_added)
        await self.sosc.connect()

    def _device_added(self, id_, type_, port):
        grid = monome.Grid()
        asyncio.create_task(self._setup_grid(grid, port))

    async def _setup_grid(self, grid, port):
        await grid.connect("127.0.0.1", port)
        while grid.id is None:
            await asyncio.sleep(0.01)
        self.grids.append(grid)
        self.offsets[grid.id] = 0 if len(self.grids) == 1 else 8
        grid.key_event.add_handler(functools.partial(self._on_key, grid))
        self._draw_monome()

    def _on_key(self, grid, x, y, s):
        if s == 0:
            return
        vx = x + self.offsets[grid.id]
        state.steps[y][vx] ^= 1
        if self.gui:
            self.gui.draw_grid()
        self._draw_monome()

    def _draw_monome(self):
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
                self._draw_monome()
                if self.gui and self.gui.canvas.winfo_exists():
                    self.gui.draw_grid()
                asyncio.create_task(self._note_off(notes))
                state.playcol = (state.playcol + 1) % COLS
            await asyncio.sleep(60 / state.bpm / 4)

    async def _note_off(self, notes):
        await asyncio.sleep((60 / state.bpm / 4) * GATE_RATIO)
        for n in notes:
            self.midi_out.send_message([0x80 | self.midi_chan, n, 0])

    def shutdown(self):
        with contextlib.suppress(Exception):
            if self.midi_out.is_port_open():
                self.midi_out.close_port()

# ---------- GUI ----------
class SequencerGUI:
    def __init__(self, root, backend):
        self.root = root
        self.backend = backend
        backend.gui = self

        root.title("Monome Sequencer")

        self.canvas = tk.Canvas(root, width=COLS * CELL_SIZE, height=ROWS * CELL_SIZE, bg="black")
        self.canvas.pack()
        self.canvas.bind("<Button-1>", self._click)

        ctrl = tk.Frame(root)
        ctrl.pack(pady=6)

        self.bpm = tk.Scale(ctrl, from_=20, to=300, orient="horizontal", label="BPM",
                            command=lambda v: setattr(state, "bpm", int(v)))
        self.bpm.set(state.bpm)
        self.bpm.grid(row=0, column=0)

        self.play_btn = tk.Button(ctrl, text="Stop", width=6, command=self._toggle)
        self.play_btn.grid(row=0, column=1, padx=6)

        self.oct = tk.Label(ctrl, text="Octave +0")
        self.oct.grid(row=0, column=2)
        tk.Button(ctrl, text="▲", width=2, command=lambda: self._shift(12)).grid(row=0, column=3)
        tk.Button(ctrl, text="▼", width=2, command=lambda: self._shift(-12)).grid(row=0, column=4)

        tk.Label(ctrl, text="MIDI Port").grid(row=1, column=0)
        self.mvar = tk.StringVar()
        self.mmenu = tk.OptionMenu(ctrl, self.mvar, *self.backend.list_ports(), command=self._set_port)
        self.mmenu.grid(row=1, column=1, columnspan=2, sticky="ew")

        tk.Button(ctrl, text="Refresh", command=self._refresh).grid(row=1, column=3)

        tk.Label(ctrl, text="Channel").grid(row=1, column=4)
        self.chan = tk.Spinbox(ctrl, from_=1, to=16, width=3, command=self._set_chan)
        self.chan.grid(row=1, column=5)
        self.chan.delete(0, "end")
        self.chan.insert(0, str(self.backend.midi_chan + 1))

        self.mvar.set(self.backend.midi_port)
        self.draw_grid()

    def _refresh(self):
        ports = self.backend.list_ports()
        menu = self.mmenu["menu"]
        menu.delete(0, "end")
        for p in ports:
            menu.add_command(label=p, command=lambda pp=p: self._set_port(pp))

        current = self.backend.midi_port
        if current not in ports:
            fallback = ports[0] if ports else "MonomeSeq (virtual)"
            self._set_port(fallback)

        self.mvar.set(self.backend.midi_port)

    def _set_port(self, name):
        self.backend.set_port(name)
        self.mvar.set(self.backend.midi_port)

    def _set_chan(self):
        self.backend.midi_chan = int(self.chan.get()) - 1

    def _toggle(self):
        state.running = not state.running
        self.play_btn.config(text="Stop" if state.running else "Play")

    def _shift(self, val):
        state.transpose += val
        octs = state.transpose // 12
        self.oct.config(text=f"Octave {octs:+}")

    def _click(self, ev):
        col = ev.x // CELL_SIZE
        row_disp = ev.y // CELL_SIZE
        y = ROWS - 1 - row_disp
        if 0 <= col < COLS and 0 <= y < ROWS:
            state.steps[y][col] ^= 1
            self.draw_grid()
            self.backend._draw_monome()

    def draw_grid(self):
        self.canvas.delete("all")
        for y in range(ROWS):
            for x in range(COLS):
                row_disp = ROWS - 1 - y
                if x == state.playcol:
                    fill = "lime green" if state.steps[y][x] else "orange"
                else:
                    fill = "white" if state.steps[y][x] else "gray20"
                self.canvas.create_rectangle(
                    x * CELL_SIZE, row_disp * CELL_SIZE,
                    (x + 1) * CELL_SIZE, (row_disp + 1) * CELL_SIZE,
                    fill=fill, outline="gray50")

# ---------- main loop ----------
async def main():
    backend = Backend()
    root = tk.Tk()
    gui = SequencerGUI(root, backend)
    await backend.start()

    try:
        while True:
            if not root.winfo_exists():
                break
            try:
                root.update()
            except tk.TclError:
                break
            await asyncio.sleep(0.01)
    except KeyboardInterrupt:
        print("Ctrl+C received. Exiting...")
    finally:
    # Graceful shutdown
        backend.shutdown()
        try:
            root.destroy()
        except tk.TclError:
            pass

if __name__ == "__main__":
        asyncio.run(main())
   