#!/usr/bin/env python3
# monome_gui_seq.py  – Dual-Monome step-sequencer with Tkinter GUI + MIDI

import asyncio
import contextlib
import functools
import tkinter as tk
import monome
import rtmidi

# ────────── CONSTANTS ─────────────────────────
ROWS, COLS = 8, 16
CELL_SIZE  = 20
BASE_NOTE  = 36
GATE_RATIO = 0.9
# ──────────────────────────────────────────────


# ────────── SEQ STATE ─────────────────────────
class SeqState:
    def __init__(self):
        self.steps      = [[0]*COLS for _ in range(ROWS)]
        self.playcol    = 0
        self.running    = True
        self.bpm        = 120
        self.transpose  = 0
state = SeqState()
# ──────────────────────────────────────────────


# ────────── BACKEND (Monome + MIDI) ───────────
class Backend:
    def __init__(self):
        self.grids, self.offsets = [], {}
        self.gui = None

        # MIDI init
        self.midi_out = rtmidi.MidiOut()
        ports = self.midi_out.get_ports()
        if ports:
            self.midi_out.open_port(0)
            self.midi_port    = ports[0]
            self.midi_virtual = False
        else:
            self.midi_out.open_virtual_port("MonomeSeq (virtual)")
            self.midi_port    = "MonomeSeq (virtual)"
            self.midi_virtual = True
        self.midi_chan = 0  # channel-1

    # ---- MIDI helpers
    def list_ports(self): return self.midi_out.get_ports()

    def set_port(self, name:str):
        if name == self.midi_port:
            return                               # no change

        # Close current port unless it's the existing virtual one
        if self.midi_out.is_port_open() and not self.midi_virtual:
            with contextlib.suppress(rtmidi.InvalidUseError):
                self.midi_out.close_port()

        ports = self.list_ports()
        if name in ports:
            idx = ports.index(name)
            self.midi_out.open_port(idx)
            self.midi_port    = name
            self.midi_virtual = False
        else:
            # fallback to virtual
            if not self.midi_virtual:
                with contextlib.suppress(rtmidi.InvalidUseError):
                    self.midi_out.open_virtual_port("MonomeSeq (virtual)")
            self.midi_port    = "MonomeSeq (virtual)"
            self.midi_virtual = True

    # ---- async tasks
    async def start(self):
        asyncio.create_task(self._serialosc())
        asyncio.create_task(self._clock())

    async def _serialosc(self):
        sosc = monome.SerialOsc()
        sosc.device_added_event.add_handler(self._grid_added)
        await sosc.connect()

    def _grid_added(self, id_, typ, port):
        g = monome.Grid()
        asyncio.create_task(self._setup_grid(g, port))

    async def _setup_grid(self, g, port):
        await g.connect("127.0.0.1", port)
        while g.id is None: await asyncio.sleep(0.01)
        off = 0 if len(self.grids)==0 else 8
        self.grids.append(g); self.offsets[g.id] = off
        g.key_event.add_handler(functools.partial(self._on_key, g))
        self._redraw()

    # ---- key + LED
    def _on_key(self, grid, x, y, s):
        if not s: return
        vx = x + self.offsets[grid.id]
        state.steps[y][vx] ^= 1
        self._redraw()
        if self.gui and self.gui.canvas.winfo_exists():
            self.gui.draw_grid()

    def _redraw(self):
        for g in self.grids:
            off = self.offsets[g.id]
            for y in range(ROWS):
                g.led_row(0, y, [state.steps[y][x] for x in range(off, off+8)])
            if off <= state.playcol < off+8:
                lx = state.playcol - off
                for y in range(ROWS):
                    g.led_set(lx, y, 1)

    # ---- clock + MIDI
    async def _clock(self):
        while True:
            if state.running:
                notes=[]
                for row in range(ROWS):
                    if state.steps[row][state.playcol]:
                        note = BASE_NOTE + state.transpose + (ROWS-1-row)
                        self.midi_out.send_message([0x90|self.midi_chan, note, 100])
                        notes.append(note)
                self._redraw()
                try:
                    if self.gui and self.gui.canvas.winfo_exists():
                        self.gui.draw_grid()
                except tk.TclError:
                    break
                asyncio.create_task(self._note_off(notes))
                state.playcol = (state.playcol+1) % COLS
            await asyncio.sleep(60/state.bpm/4)

    async def _note_off(self, notes):
        await asyncio.sleep((60/state.bpm/4)*GATE_RATIO)
        for n in notes:
            self.midi_out.send_message([0x80|self.midi_chan, n, 0])

    # ---- cleanup
    def shutdown(self):
        if self.midi_out.is_port_open():
            with contextlib.suppress(rtmidi.InvalidUseError):
                self.midi_out.close_port()
        for g in self.grids:
            with contextlib.suppress(Exception): g.disconnect()
# ──────────────────────────────────────────────


# ────────── GUI ───────────────────────────────
class SequencerGUI:
    def __init__(self, root:tk.Tk, backend:Backend):
        self.backend=backend; backend.gui=self
        root.title("Monome Sequencer")

        self.canvas=tk.Canvas(root,width=COLS*CELL_SIZE,height=ROWS*CELL_SIZE,bg="black")
        self.canvas.pack(); self.canvas.bind("<Button-1>",self._click)

        ctrl=tk.Frame(root); ctrl.pack(pady=6)

        self.bpm=tk.Scale(ctrl,label="BPM",from_=20,to=300,orient="horizontal",
                          command=lambda v:setattr(state,"bpm",int(float(v))))
        self.bpm.set(state.bpm); self.bpm.grid(row=0,column=0)

        self.play=tk.Button(ctrl,text="Stop",width=6,command=self._toggle)
        self.play.grid(row=0,column=1,padx=6)

        self.oct=tk.Label(ctrl,text="Octave +0"); self.oct.grid(row=0,column=2)
        tk.Button(ctrl,text="▲",width=2,command=lambda:self._shift(12)).grid(row=0,column=3)
        tk.Button(ctrl,text="▼",width=2,command=lambda:self._shift(-12)).grid(row=0,column=4)

        # MIDI port dropdown
        tk.Label(ctrl,text="MIDI Port").grid(row=1,column=0)
        ports = backend.list_ports() or ["No devices"]
        self.mvar = tk.StringVar(value=backend.midi_port)
        self.mmenu = tk.OptionMenu(ctrl, self.mvar, *ports, command=self._set_port)
        self.mmenu.grid(row=1,column=1,columnspan=2,sticky="ew")
        tk.Button(ctrl,text="Refresh",command=self._refresh).grid(row=1,column=3)

        tk.Label(ctrl,text="Ch").grid(row=1,column=4)
        self.ch = tk.Spinbox(ctrl,from_=1,to=16,width=3,command=self._set_ch)
        self.ch.delete(0,"end"); self.ch.insert(0,backend.midi_chan+1)
        self.ch.grid(row=1,column=5)

        self.draw_grid()

    # ---- draw grid
    def draw_grid(self):
        self.canvas.delete("all")
        for y in range(ROWS):
            for x in range(COLS):
                rd=ROWS-1-y
                fill="lime green" if x==state.playcol and state.steps[y][x]\
                    else "dark green" if x==state.playcol\
                    else "white" if state.steps[y][x] else "gray20"
                self.canvas.create_rectangle(
                    x*CELL_SIZE,rd*CELL_SIZE,(x+1)*CELL_SIZE,(rd+1)*CELL_SIZE,
                    fill=fill,outline="gray50")

    # ---- mouse click
    def _click(self,ev):
        col=ev.x//CELL_SIZE; rd=ev.y//CELL_SIZE; y=ROWS-1-rd
        if 0<=col<COLS and 0<=y<ROWS:
            state.steps[y][col]^=1
            self.draw_grid(); self.backend._redraw()

    # ---- controls
    def _toggle(self):
        state.running=not state.running
        self.play.config(text="Stop" if state.running else "Play")
    def _shift(self,s):
        state.transpose+=s; self.oct.config(text=f"Octave {state.transpose//12:+d}")
    def _set_port(self,name): self.backend.set_port(name); self.mvar.set(self.backend.midi_port)
    def _set_ch(self):
        try: self.backend.midi_chan=max(0,min(15,int(self.ch.get())-1))
        except ValueError: pass

    # refresh MIDI menu
    def _refresh(self):
        ports = self.backend.list_ports()
        menu  = self.mmenu["menu"]; menu.delete(0,"end")
        if not ports:
            ports=["MonomeSeq (virtual)"]
        for p in ports:
            menu.add_command(label=p,command=lambda pp=p:self._set_port(pp))
        if self.mvar.get() not in ports:
            self._set_port(ports[0])
        self.mvar.set(self.backend.midi_port)
# ──────────────────────────────────────────────


# ────────── MAIN (asyncio.run) ─────────────────
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
        try:
            backend.shutdown()
        except Exception as e:
            print(f"Backend shutdown error: {e}")
        with contextlib.suppress(tk.TclError):
            try:
                if root.winfo_exists():
                    root.destroy()
            except Exception as e:
                print(f"Error destroying root: {e}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"Fatal error in main loop: {e}")
