#!/usr/bin/env python3
# monome_gui_seq.py
# -----------------------------------------------------------
# A dual-Monome step sequencer with Tkinter GUI and MIDI out.
# -----------------------------------------------------------

import tkinter as tk
import asyncio, functools, signal, contextlib
import monome
import rtmidi

# ---------- CONSTANTS -------------------------------------
ROWS       = 8    # number of rows in the (virtual) grid
COLS       = 16   # number of columns in the (virtual) grid
CELL_SIZE  = 20   # pixel size of each GUI cell
BASE_NOTE  = 36   # MIDI note for bottom-left cell (C2)
GATE_RATIO = 0.9  # fraction of step length held as note-on
# -----------------------------------------------------------


# ---------- GLOBAL SEQUENCER STATE -------------------------
class SeqState:
    """Holds runtime-mutable sequencer parameters."""
    def __init__(self):
        self.steps      = [[0]*COLS for _ in range(ROWS)]
        self.playcol    = 0
        self.running    = True
        self.bpm        = 120
        self.transpose  = 0
state = SeqState()
# -----------------------------------------------------------


# ---------- BACKEND  (Monome + MIDI) -----------------------
class Backend:
    #Handles serialosc discovery, Monome I/O and MIDI output.
    def __init__(self):
        # MIDI setup -------------------------------------------------
        self.midi_out   = rtmidi.MidiOut()
        self.midi_port  = None                       # human-readable name
        self.midi_chan  = 0                          # 0 = MIDI channel-1
        ports = self.midi_out.get_ports()
        if ports:
            self.midi_out.open_port(0)
            self.midi_port = ports[0]
        else:
            self.midi_out.open_virtual_port("MonomeSeq")
            self.midi_port = "MonomeSeq (virtual)"
        # Monome tracking -------------------------------------------
        self.grids   : list[monome.Grid] = []
        self.offsets : dict[str,int]     = {}        # grid.id → 0 or 8
        # GUI link (filled later)
        self.gui = None

    def shutdown(self):
        if self.midi_out.is_port_open():
            self.midi_out.close_port()


    # ----- MIDI helpers ------------------------------------
    def list_ports(self):
        """Return a list of available MIDI output port names."""
        return self.midi_out.get_ports()

    def set_port(self, name: str):
        if name == self.midi_port:
            return  # Already selected

        # Close current port safely
        if self.midi_out.is_port_open():
            with contextlib.suppress(rtmidi.InvalidUseError):
                self.midi_out.close_port()

        ports = self.list_ports()
        for i, p in enumerate(ports):
            if p == name:
                self.midi_out.open_port(i)
                self.midi_port = name
                return

        # Fall back to virtual port
        with contextlib.suppress(rtmidi.InvalidUseError):
            self.midi_out.open_virtual_port("MonomeSeq (virtual)")
        self.midi_port = "MonomeSeq (virtual)"


    # ----- serialosc / grids --------------------------------
    async def start(self):
        asyncio.create_task(self._serialosc_connect())
        asyncio.create_task(self._clock_loop())

    async def _serialosc_connect(self):
        self.sosc = monome.SerialOsc()
        self.sosc.device_added_event.add_handler(self._grid_added)
        await self.sosc.connect()

    def _grid_added(self, id_, typ, port):
        grid = monome.Grid()
        asyncio.create_task(self._setup_grid(grid, port))

    async def _setup_grid(self, grid: monome.Grid, port):
        await grid.connect("127.0.0.1", port)
        while grid.id is None:
            await asyncio.sleep(0.01)
        # assign horizontal offset
        off = 0 if len(self.grids)==0 else 8
        self.grids.append(grid)
        self.offsets[grid.id] = off
        grid.key_event.add_handler(functools.partial(self._on_key, grid))
        self._redraw_monome()

    # ----- Monome key handling ------------------------------
    def _on_key(self, grid, x, y, s):
        if s == 0:
            return                      # ignore key-release
        vx = x + self.offsets[grid.id]  # virtual col
        state.steps[y][vx] ^= 1
        self._redraw_monome()
        if self.gui:
            self.gui.draw_grid()

    # ----- LED refresh --------------------------------------
    def _redraw_monome(self):
        for g in self.grids:
            off = self.offsets[g.id]
            for y in range(ROWS):
                g.led_row(0, y,
                          [state.steps[y][x] for x in range(off, off+8)])
            # draw playhead overlay
            if off <= state.playcol < off+8:
                lx = state.playcol - off
                for y in range(ROWS):
                    g.led_set(lx, y, 1)

    # ----- Sequencer clock + MIDI ---------------------------
    async def _clock_loop(self):
        while True:
            if state.running:
                # send note-ons for active steps
                active_notes = []
                for row in range(ROWS):
                    if state.steps[row][state.playcol]:
                        note = BASE_NOTE + state.transpose + (ROWS-1-row)
                        self.midi_out.send_message(
                            [0x90 | self.midi_chan, note, 100])
                        active_notes.append(note)

                # update visuals
                self._redraw_monome()
            try:
                if self.gui and self.gui.canvas.winfo_exists():
                    self.gui.draw_grid()
            except tk.TclError:
                return  # GUI was destroyed

                # schedule note-offs
                asyncio.create_task(
                    self._note_off_after(active_notes))

                # advance playhead
                state.playcol = (state.playcol + 1) % COLS

            await asyncio.sleep(60/state.bpm/4)   # 16th-note interval

    async def _note_off_after(self, notes):
        await asyncio.sleep((60/state.bpm/4)*GATE_RATIO)
        for n in notes:
            self.midi_out.send_message([0x80 | self.midi_chan, n, 0])
# -----------------------------------------------------------


# ---------- TKINTER GUI ------------------------------------
class SequencerGUI:
    def __init__(self, root: tk.Tk, backend: Backend):
        self.root     = root
        self.backend  = backend
        backend.gui   = self          # circular link for callbacks

        root.title("Monome Sequencer")

        # --- grid canvas -----------------------------------
        self.canvas = tk.Canvas(root, width=COLS*CELL_SIZE,
                                height=ROWS*CELL_SIZE, bg="black")
        self.canvas.pack()
        self.canvas.bind("<Button-1>", self._canvas_click)

        # --- control frame ---------------------------------
        ctrl = tk.Frame(root); ctrl.pack(pady=6)

        # BPM slider
        self.bpm_slider = tk.Scale(ctrl, from_=20, to=300, orient="horizontal",
                                   label="BPM", command=self._set_bpm)
        self.bpm_slider.set(state.bpm)
        self.bpm_slider.grid(row=0, column=0, columnspan=2, sticky="ew")

        # Play / Stop
        self.play_btn = tk.Button(ctrl, text="Stop", width=6,
                                  command=self._toggle_play)
        self.play_btn.grid(row=0, column=2, padx=6)

        # Octave shift
        self.oct_label = tk.Label(ctrl, text="Octave +0")
        self.oct_label.grid(row=0, column=3)
        tk.Button(ctrl, text="▲", width=2,
                  command=lambda: self._shift_oct(12)).grid(row=0, column=4)
        tk.Button(ctrl, text="▼", width=2,
                  command=lambda: self._shift_oct(-12)).grid(row=0, column=5)

        # MIDI port selector
        tk.Label(ctrl, text="MIDI Port").grid(row=1, column=0)

        ports = self.backend.list_ports()
        if not ports:                         # if nothing is reported
            ports = ["MonomeSeq (virtual)"]   # show the virtual port name

        self.midi_var = tk.StringVar(value=self.backend.midi_port or ports[0])

        self.midi_menu = tk.OptionMenu(
            ctrl,
            self.midi_var,
            *ports,
            command=self._set_midi_port
        )
        self.midi_menu.grid(row=1, column=1, columnspan=2, sticky="ew")

        # Refresh MIDI port list
        tk.Button(ctrl, text="Refresh MIDI", command=self.refresh_midi_ports).grid(row=1, column=5, padx=4)


        # MIDI channel selector
        tk.Label(ctrl, text="Ch").grid(row=1, column=3)
        self.ch_spin = tk.Spinbox(ctrl, from_=1, to=16, width=3,
                                  command=self._set_midi_channel)
        self.ch_spin.delete(0, "end")
        self.ch_spin.insert(0, str(self.backend.midi_chan+1))
        self.ch_spin.grid(row=1, column=4)

        self.draw_grid()

    # ----- canvas drawing ----------------------------------
    def draw_grid(self):
        self.canvas.delete("all")
        for y in range(ROWS):
            for x in range(COLS):
                row_disp = ROWS-1-y
                if x == state.playcol:
                    fill = "lime green" if state.steps[y][x] else "dark green"
                else:
                    fill = "white" if state.steps[y][x] else "gray20"
                self.canvas.create_rectangle(
                    x*CELL_SIZE, row_disp*CELL_SIZE,
                    (x+1)*CELL_SIZE, (row_disp+1)*CELL_SIZE,
                    fill=fill, outline="gray50")

    def _canvas_click(self, ev):
        col = ev.x // CELL_SIZE
        row_disp = ev.y // CELL_SIZE
        y = ROWS-1-row_disp
        if 0<=col<COLS and 0<=y<ROWS:
            state.steps[y][col] ^= 1
            self.draw_grid()
            self.backend._redraw_monome()

    # ----- GUI control callbacks ---------------------------
    def _set_bpm(self, val):  state.bpm = int(float(val))

    def _toggle_play(self):
        state.running = not state.running
        self.play_btn.config(text="Stop" if state.running else "Play")

    def _shift_oct(self, semis):
        state.transpose += semis
        self.oct_label.config(text=f"Octave {state.transpose//12:+d}")

    def _set_midi_port(self, name):
        self.backend.set_port(name)

    def _set_midi_channel(self):
        ch = max(1, min(16, int(self.ch_spin.get())))
        self.backend.midi_chan = ch-1

    def refresh_midi_ports(self):
        ports = self.backend.list_ports()
        if not ports:
            ports = ["MonomeSeq (virtual)"]

        menu = self.midi_menu["menu"]
        menu.delete(0, "end")  # clear old entries

        for port in ports:
            menu.add_command(label=port, command=lambda p=port: self._set_midi_port(p))

        # update current value if it's no longer valid
        current = self.midi_var.get()
        if current not in ports:
            self.midi_var.set(ports[0])
            self._set_midi_port(ports[0])
# -----------------------------------------------------------


# ---------- CO-OPERATIVE TK / ASYNCIO LOOP -----------------
def _tk_async_bridge(root, loop):
    loop.call_soon(loop.stop)
    loop.run_forever()
    root.after(10, _tk_async_bridge, root, loop)
# -----------------------------------------------------------


# ---------- MAIN -------------------------------------------
async def main():
    backend = Backend()               # create backend first
    root = tk.Tk()
    gui   = SequencerGUI(root, backend)
    await backend.start()

    # integrate Tkinter into asyncio
    try:
        while True:
            try:
                if not root.winfo_exists():
                    break
                root.update()
            except tk.TclError:
                break  # Tkinter app was destroyed
            await asyncio.sleep(0.01)
    except KeyboardInterrupt:
        print("Ctrl+C pressed – exiting")
    finally:
        backend.shutdown()
        try:
            root.destroy()
        except tk.TclError:
            pass  # already closed

    # handle Ctrl-C gracefully
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, loop.stop)

    root.after(10, _tk_async_bridge, root, loop)
    try:
        root.mainloop()
    finally:
        loop.stop()
        with contextlib.suppress(asyncio.CancelledError):
            loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()

# -----------------------------------------------------------
if __name__ == "__main__":
    asyncio.run(main())
