import asyncio, functools, contextlib, signal
import tkinter as tk
import monome, rtmidi

# ---------- constants ----------
ROWS, COLS = 8, 16           # Grid size
CELL_SIZE = 20               # GUI cell size in pixels
BASE_NOTE = 36               # MIDI note for bottom-left cell
GATE_RATIO = 0.9             # Note duration as fraction of step
PAGES = 4  # Number of pages

# ---------- global state ----------
class SeqState:
    def __init__(self):
        self.current_page = 0
        self.total_pages = 4  # Change this to however many pages you want
        self.steps = [
            [[0] * COLS for _ in range(ROWS)]
            for _ in range(self.total_pages)
        ]
        self.playcol = 0
        self.running = True
        self.bpm = 120
        self.transpose = 0
        self.clock_mode = "internal"
        self.ticks = 0
        self.swing = 0.0  # Swing amount, 0.0 = no swing, 0.5 = max swing (50% delay on odd steps)


    # page helpers
    def next_page(self):
        self.current_page = (self.current_page+1) % self.total_pages
    def prev_page(self):
        self.current_page = (self.current_page-1) % self.total_pages
    def step_active(self,y,x):
        return self.steps[self.current_page][y][x]

state = SeqState()

# ---------- backend ----------
class Backend:
    def __init__(self):
        self.grids, self.offsets = [], {}
        self.gui = None
        self.clock_task = None

        # MIDI Out
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

        # MIDI In
        self.midi_in = rtmidi.MidiIn()
        self.midi_in.ignore_types(False, False)
        self.midi_in.set_callback(self._midi_in_cb)
        in_ports = self.midi_in.get_ports()
        if in_ports:
            self.midi_in.open_port(0)

    def _midi_in_cb(self, msg, _):
        if state.clock_mode != "receive":
            return
        status = msg[0][0]
        if status == 0xF8:  # MIDI Clock
            self._clock_pulse()
        elif status == 0xFA:  # Start
            state.running = True
            state.tick_count = 0
        elif status == 0xFC:  # Stop
            state.running = False

    def _midi_clock_in(self,msg,_):
        if state.clock_mode != "receive": return
        b = msg[0][0]
        if b==0xFA:     # Start
            state.running=True; state.tick_count=0
        elif b==0xFC:   # Stop
            state.running=False
        elif b==0xF8 and state.running:   # Clock pulse
            state.tick_count = (state.tick_count+1) % 6
            if state.tick_count==0:
                asyncio.create_task(self._step())

    def list_ports(self):
        return self.midi_out.get_ports()

    def set_port(self, name):
        if name == self.midi_port:
            return
        try: self.midi_out.close_port()
        except: pass
        self.midi_out = rtmidi.MidiOut()
        for i, p in enumerate(self.midi_out.get_ports()):
            if p == name:
                try:
                    self.midi_out.open_port(i)
                    self.midi_port = name
                    self.midi_virtual = False
                    return
                except Exception as e:
                    print(f"Failed to open MIDI port '{name}':", e)
        try:
            self.midi_out.open_virtual_port("MonomeSeq (virtual)")
            self.midi_port = "MonomeSeq (virtual)"
            self.midi_virtual = True
        except Exception as e:
            print("Failed to open virtual port:", e)

    async def start(self):
        await self._connect_serialosc()
        if state.clock_mode != "receive":
            self.clock_task = asyncio.create_task(self._clock_loop())

    async def _connect_serialosc(self):
        self.sosc = monome.SerialOsc()
        self.sosc.device_added_event.add_handler(self._device_added)
        await self.sosc.connect()

    def _device_added(self, id_, typ, port):
        g = monome.Grid()
        asyncio.create_task(self._setup_grid(g, port))

    async def _setup_grid(self, grid, port):
        await grid.connect("127.0.0.1", port)
        while grid.id is None:
            await asyncio.sleep(0.01)
        self.grids.append(grid)
        self.offsets[grid.id] = 0 if len(self.grids) == 1 else 8
        grid.key_event.add_handler(functools.partial(self._on_key, grid))
        self.redraw_monome()

    def _on_key(self, grid, x, y, s):
        if s == 0: return
        vx = x + self.offsets[grid.id]
        vy = ROWS - 1 - y
        state.steps[state.current_page][vy][vx] ^= 1
        if self.gui: self.gui.draw_grid()
        self.redraw_monome()

    def redraw_monome(self):
        for g in self.grids:
            off = self.offsets[g.id]
            for y in range(ROWS):
                row = []
                for x in range(off, off+8):
                    on = state.step_active(y,x) or (x==state.playcol)
                    row.append(1 if on else 0)
                g.led_row(0, ROWS-1-y, row)

    async def _clock_loop(self):
        while True:
            if state.running and state.clock_mode in ("internal","send"):
                await self._step()
                if state.clock_mode=="send":
                    for _ in range(6): self.midi_out.send_message([0xF8])
            step_time = 60 / state.bpm / 4
            # Delay odd steps by swing amount
            if state.playcol % 2 == 1:
                await asyncio.sleep(step_time * (1 + state.swing))
            else:
                await asyncio.sleep(step_time * (1 - state.swing))


    def _clock_pulse(self):
        if not state.running:
            return
        state.tick_count += 1
        if state.tick_count >= 6:
            state.tick_count = 0
            asyncio.create_task(self._step())

    async def _step(self):
        notes=[]
        for r in range(ROWS):
            if state.step_active(r,state.playcol):
                n = BASE_NOTE+state.transpose+(ROWS-1-r)
                self.midi_out.send_message([0x90|self.midi_chan,n,100]); notes.append(n)
        self.redraw_monome()
        if self.gui and self.gui.canvas.winfo_exists(): self.gui.draw_grid()
        asyncio.create_task(self._note_off(notes))
        state.playcol = (state.playcol+1)%COLS

    async def _note_off(self, notes):
        await asyncio.sleep((60 / state.bpm / 4) * GATE_RATIO)
        for note in notes:
            self.midi_out.send_message([0x80 | self.midi_chan, note, 0])

    def shutdown(self):
        try: self.midi_out.close_port()
        except: pass
        try: self.midi_in.close_port()
        except: pass

    def steps(self):
        return self.pages[self.current_page]

    def toggle_step(self, y, x):
        self.steps[y][x] ^= 1

    def next_page(self):
        self.current_page = (self.current_page + 1) % PAGES

    def prev_page(self):
        self.current_page = (self.current_page - 1) % PAGES    

# ---------- GUI ----------
class SequencerGUI:
    def __init__(self, root: tk.Tk, backend: Backend):
        label_f  = ("Helvetica", 12)
        button_f = ("Helvetica", 12, "bold")

        self.backend = backend
        backend.gui  = self

        root.configure(bg="#222")
        root.title("Monome Sequencer")

        # --- canvas grid ---
        self.canvas = tk.Canvas(root, width=COLS*CELL_SIZE, height=ROWS*CELL_SIZE,
                                bg="#222", highlightthickness=0)
        self.canvas.pack(padx=8, pady=(8,4))
        self.canvas.bind("<Button-1>", self._click)

        # master frame for controls
        ctrl = tk.Frame(root, bg="#222")
        ctrl.pack(padx=8, pady=8, anchor="w")

        # ── Transport frame
        trans = tk.Frame(ctrl, bg="#222")
        trans.grid(row=0, column=0, sticky="w")

        tk.Label(trans, text="BPM", font=label_f, fg="#ddd", bg="#222").pack(side="left", padx=4)
        self.bpm = tk.Scale(trans, from_=40, to=300, orient="horizontal", length=120,
                            command=self._bpm, bg="#222", fg="#ddd", troughcolor="#444",
                            highlightthickness=0)
        self.bpm.set(state.bpm)
        self.bpm.pack(side="left")

        self.play = tk.Button(ctrl, text="Stop", width=6, command=self._toggle, bg="white", fg="red")
        self.play.grid(row=0, column=3, padx=4)
        ctrl.grid_columnconfigure(1, weight=1)
        ctrl.grid_columnconfigure(2, weight=1)


        # ── Pitch + Swing
        pitch = tk.Frame(ctrl, bg="#222")
        pitch.grid(row=1, column=0, sticky="w", pady=(6,0))

        self.oct = tk.Label(pitch, text="Oct  +0", font=label_f, fg="#ddd", bg="#222")
        self.oct.pack(side="left", padx=(0,6))
        tk.Button(pitch, text="▲", font=button_f, width=2,
                  command=lambda:self._oct(12)).pack(side="left")
        tk.Button(pitch, text="▼", font=button_f, width=2,
                  command=lambda:self._oct(-12)).pack(side="left", padx=(0,8))

        tk.Label(pitch, text="Swing", font=label_f, fg="#ddd", bg="#222").pack(side="left")
        self.swing = tk.Scale(pitch, from_=0, to=50, orient="horizontal", length=100,
                              command=self._set_swing, bg="#222", fg="#ddd",
                              troughcolor="#444", highlightthickness=0)
        self.swing.set(int(state.swing*100))
        self.swing.pack(side="left", padx=(0,4))

        # ── Clock & MIDI frame
        mid = tk.Frame(ctrl, bg="#222")
        mid.grid(row=2, column=0, sticky="w", pady=(6,0))

        tk.Label(mid, text="Clock", font=label_f, fg="#ddd", bg="#222").pack(side="left", padx=4)
        self.mode = tk.StringVar(value=state.clock_mode)
        tk.OptionMenu(mid, self.mode, "internal", "send", "receive",
                      command=self._set_mode).pack(side="left")

        tk.Label(mid, text="MIDI-Out", font=label_f, fg="#ddd", bg="#222").pack(side="left", padx=(12,4))
        self.port = tk.StringVar(value=backend.midi_out.get_ports()[0]
                                 if backend.midi_out.get_ports() else backend.out_name)
        tk.OptionMenu(mid, self.port, *backend.midi_out.get_ports(),
                      command=self._set_port).pack(side="left")

        tk.Label(mid, text="Ch", font=label_f, fg="#ddd", bg="#222").pack(side="left", padx=(12,2))
        self.ch = tk.Spinbox(mid, from_=1, to=16, width=3, command=self._set_ch)
        self.ch.delete(0,"end"); self.ch.insert(0, backend.midi_chan+1)
        self.ch.pack(side="left")

        # ── Page controls
        pages = tk.Frame(ctrl, bg="#222")
        pages.grid(row=3, column=0, sticky="w", pady=(6,0))

        tk.Button(pages, text="◀", font=button_f, width=2,
                  command=self.prev_page).pack(side="left")
        self.page_label = tk.Label(pages, text=f"Page {state.current_page+1}/{PAGES}",
                                   font=label_f, fg="#ddd", bg="#222")
        self.page_label.pack(side="left", padx=4)
        tk.Button(pages, text="▶", font=button_f, width=2,
                  command=self.next_page).pack(side="left")

        self.draw_grid()

    def draw_grid(self):
        c = self.canvas
        c.delete("all")
        for y in range(ROWS):
            disp = ROWS-1-y
            for x in range(COLS):
                on = state.step_active(y,x)
                is_play = (x == state.playcol)
                fill = "#FFEE00" if is_play and on else \
                       "#4444FF" if is_play else \
                       "#FFA53F" if on else "#444"
                x0, y0 = x*CELL_SIZE, disp*CELL_SIZE
                x1, y1 = x0+CELL_SIZE, y0+CELL_SIZE
                c.create_oval(x0+2, y0+2, x1-2, y1-2, fill=fill, outline="#333")

    def _set_swing(self, val):
        state.swing = int(val) / 100.0

    def _click(self, ev):
        col = ev.x // CELL_SIZE
        row_disp = ev.y // CELL_SIZE
        y = ROWS - 1 - row_disp
        if 0 <= col < COLS and 0 <= y < ROWS:
            state.steps[state.current_page][y][col] ^= 1
            self.draw_grid()
            self.backend.redraw_monome()

    def _bpm(self, val):
        state.bpm = int(float(val))

    def _toggle(self):
        state.running = not state.running
        if state.running:
            self.play.config(text="Stop", bg="white", fg="red")
        else:
            self.play.config(text="Play", bg="white", fg="green")

    def _oct(self, shift):
        state.transpose += shift
        self.oct.config(text=f"Octave {state.transpose // 12:+}")

    def _set_chan(self):
        ch = max(1, min(16, int(self.chan.get()))) - 1
        state.cur.midi_chan = ch


    def _set_port(self, name):
        """Apply user choice to backend and dropdown variable."""
        self.be.set_port(name)
        self.port.set(name)            # reflect choice in UI


    def _refresh(self):
        ports = self.backend.list_ports()
        menu = self.mmenu["menu"]
        menu.delete(0, "end")
        for p in ports:
            menu.add_command(label=p, command=lambda val=p: self._set_port(val))
        fallback = ports[0] if ports else "MonomeSeq (virtual)"
        self._set_port(fallback)

    def next_page(self):
        state.next_page()
        self.page_label.config(text=f"Page {state.current_page+1}/{PAGES}")
        self.draw_grid()
        self.backend.redraw_monome()

    def prev_page(self):
        state.prev_page()
        self.page_label.config(text=f"Page {state.current_page+1}/{PAGES}")
        self.draw_grid()
        self.backend.redraw_monome()

    def _set_mode(self, mode):
        state.clock_mode = mode

# ---------- main ----------
async def main():
    root = tk.Tk()
    backend = Backend()
    gui = SequencerGUI(root, backend)
    await backend.start()

    try:
        while root.winfo_exists():
            root.update()
            await asyncio.sleep(0.01)
    except KeyboardInterrupt:
        print("Exiting...")
    finally:
        with contextlib.suppress(Exception): root.destroy()
        backend.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
