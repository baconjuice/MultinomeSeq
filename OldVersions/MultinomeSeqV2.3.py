#!/usr/bin/env python3
# multinome_seq_tracks.py  –  4-track Monome sequencer (2025-07-07)

import asyncio, functools, contextlib, queue, threading
import tkinter as tk
import monome, rtmidi

# ───────── constants ─────────────────────────────────────────
ROWS, COLS  = 8, 16
CELL_SIZE   = 20
BASE_NOTE   = 36
GATE_RATIO  = 0.9
TRACKS      = 4
VEL_DEF     = 100          # velocity set by normal click
VEL_INC     = 15           # velocity increase on shift-click
VEL_OFF, VEL_LOW, VEL_MED, VEL_HI = 0, 40, 80, 127
VEL_LEVELS = [VEL_OFF, VEL_LOW, VEL_MED, VEL_HI]
# ────────────────────────────────────────────────────────────

# ───────── data classes ─────────────────────────────────────
class Track:
    def __init__(self, name, midi_chan=0):
        self.name       = name
        self.steps      = [[0]*COLS for _ in range(ROWS)]   # 0 = off, 1-127 = velocity
        self.playcol    = 0
        self.midi_chan  = midi_chan
        self.transpose  = 0          # ← NEW • semitone offset
        self.mute       = False

class SeqState:
    def __init__(self):
        self.tracks     = [Track(f"T{i+1}", i) for i in range(TRACKS)]
        self.cur_idx    = 0
        self.running    = True
        self.bpm        = 120
        self.swing      = 0.0
        self.clock_mode = "internal"   # internal | send | receive
        self.tick_count = 0            # ext-clock counter

    @property
    def cur(self): return self.tracks[self.cur_idx]

state = SeqState()
# ────────────────────────────────────────────────────────────

# ───────── backend (Monome + threaded MIDI) ────────────────
class Backend:
    def __init__(self):
        # ---- MIDI-out with worker thread ----
        self.midi_out = rtmidi.MidiOut()
        outs = self.midi_out.get_ports()
        (self.midi_out.open_port if outs
         else self.midi_out.open_virtual_port)(0 if outs else "MonomeSeq Out")
        self.midi_q   = queue.Queue()
        threading.Thread(target=self._midi_worker, daemon=True).start()

        # ---- MIDI-in for ext clock ----
        self.midi_in = rtmidi.MidiIn()
        self.midi_in.ignore_types(False, False)
        self.midi_in.set_callback(self._clock_in)
        ins = self.midi_in.get_ports()
        if ins: self.midi_in.open_port(0)

        # ---- Monome ----
        self.grids, self.offsets, self.gui = [], {}, None

    # threaded sender
    def _midi_worker(self):
        while True:
            msg = self.midi_q.get()
            if msg is None: break
            with contextlib.suppress(Exception):
                self.midi_out.send_message(msg)

    def qmsg(self,*b): self.midi_q.put(list(b))

    # reopen selected MIDI port
    def set_port(self, name:str):
        with contextlib.suppress(Exception): self.midi_out.close_port()
        self.midi_out = rtmidi.MidiOut()
        outs = self.midi_out.get_ports()
        for i,p in enumerate(outs):
            if p == name:
                self.midi_out.open_port(i); return
        self.midi_out.open_virtual_port("MonomeSeq Out (virtual)")

    # ---- SerialOSC / grid setup ----
    async def start(self):
        asyncio.create_task(self._serialosc())
        asyncio.create_task(self._clock_loop())

    async def _serialosc(self):
        s=monome.SerialOsc()
        s.device_added_event.add_handler(self._grid_added)
        await s.connect()

    def _grid_added(self,i,t,port):
        g=monome.Grid(); asyncio.create_task(self._setup_grid(g,port))

    async def _setup_grid(self,g,port):
        await g.connect("127.0.0.1",port)
        while g.id is None: await asyncio.sleep(0.01)
        off = 0 if not self.grids else 8
        self.grids.append(g); self.offsets[g.id]=off
        g.key_event.add_handler(functools.partial(self._on_key,g))
        self.redraw_monome()

    # ---- key handler ----
    def _on_key(self,g,x,y,s):
        if not s: return
        vx=x+self.offsets[g.id]; vy=ROWS-1-y
        state.cur.steps[vy][vx]^=1
        if self.gui and self.gui.canvas.winfo_exists(): self.gui.draw_grid()
        self.redraw_monome()

    # ---- flicker-free LED redraw (selected track only) ----
    def redraw_monome(self):
        for g in self.grids:
            off=self.offsets[g.id]
            for y in range(ROWS):
                row=[0]*8
                for x in range(off,off+8):
                    if state.cur.steps[y][x]: row[x-off]=1
                for tr in state.tracks:
                    if tr.mute: continue
                    if off<=tr.playcol<off+8: row[tr.playcol-off]=1
                g.led_row(0,ROWS-1-y,row)

    # ---- MIDI clock IN ----
    def _clock_in(self,event,_):
        if state.clock_mode!="receive": return
        b=event[0][0]
        if b==0xFA: state.running=True; state.tick_count=0
        elif b==0xFC: state.running=False
        elif b==0xF8 and state.running:
            state.tick_count=(state.tick_count+1)%6
            if state.tick_count==0: asyncio.create_task(self._step())

    # ---- master clock loop ----
    async def _clock_loop(self):
        while True:
            if state.running and state.clock_mode in ("internal","send"):
                await self._step()
                if state.clock_mode=="send":
                    for _ in range(6): self.qmsg(0xF8)
            step=60/state.bpm/4
            sw=state.swing
            delay=step*(1+sw) if state.cur.playcol%2 else step*(1-sw)
            await asyncio.sleep(delay)

    # ---- advance all tracks ----
    async def _step(self):
        for tr in state.tracks:
            if tr.mute:
                tr.playcol=(tr.playcol+1)%COLS; continue
            notes=[]
            for r in range(ROWS):
                if tr.steps[r][tr.playcol]:
                    n=BASE_NOTE + tr.transpose + (ROWS-1-r)
                    self.qmsg(0x90|tr.midi_chan,n,100); notes.append(n)
            asyncio.create_task(self._note_off(notes,tr.midi_chan))
            tr.playcol=(tr.playcol+1)%COLS
        self.redraw_monome()
        if self.gui and self.gui.canvas.winfo_exists(): self.gui.draw_grid()

    async def _note_off(self,ns,ch):
        await asyncio.sleep((60/state.bpm/4)*GATE_RATIO)
        for n in ns: self.qmsg(0x80|ch,n,0)

    def shutdown(self):
        self.midi_q.put(None)
        with contextlib.suppress(Exception): self.midi_out.close_port()
        with contextlib.suppress(Exception): self.midi_in.close_port()

# ───────── GUI ─────────────────────────────────────────────
class SequencerGUI:
    def __init__(self,root,be:Backend):
        self.be=be; be.gui=self
        root.configure(bg="#222"); root.title("Monome Seq Tracks")

        # --- Grid canvas ---
        self.canvas=tk.Canvas(root,width=COLS*CELL_SIZE,height=ROWS*CELL_SIZE,
                              bg="#222",highlightthickness=0)
        self.canvas.pack(padx=8,pady=(8,4))
        self.canvas.bind("<Button-1>",self._click)

        # --- Control frame ---
        ctrl=tk.Frame(root,bg="#222"); ctrl.pack(padx=8,pady=6,anchor="w")
        LF,BF=("Helvetica",10),("Helvetica",10,"bold")

        # row0 BPM + Play
        tk.Label(ctrl,text="BPM",font=LF,fg="#ddd",bg="#222")\
          .grid(row=0,column=0,sticky="e")
        self.bpm=tk.Scale(ctrl,from_=40,to=300,orient="horizontal",length=120,
                          command=lambda v:setattr(state,"bpm",int(float(v))),
                          bg="#222",fg="#ddd", troughcolor="#444",
                          highlightthickness=0)
        self.bpm.set(state.bpm); self.bpm.grid(row=0,column=1,columnspan=2,sticky="we")
        self.play=tk.Button(ctrl,text="Stop",width=6,font=BF,bg="green",fg="red",
                            command=self._toggle)
        self.play.grid(row=0,column=3,padx=4)
        ctrl.grid_columnconfigure(1,weight=1)

        # row1 Octave + Swing
        self.oct = tk.Label(ctrl,text=f"Oct {state.cur.transpose//12:+d}",font=LF, fg="#ddd", bg="#222")
        self.oct.grid(row=1,column=0,sticky="w")
        tk.Button(ctrl,text="▲",font=BF,width=2,command=lambda:self._oct(12))\
          .grid(row=1,column=1,sticky="w")
        tk.Button(ctrl,text="▼",font=BF,width=2,command=lambda:self._oct(-12))\
          .grid(row=1,column=2,sticky="w")
        tk.Label(ctrl,text="Swing",font=LF,fg="#ddd",bg="#222")\
          .grid(row=1,column=3,sticky="e")
        self.swing=tk.Scale(ctrl,from_=0,to=50,orient="horizontal",length=100,
                            command=lambda v:setattr(state,"swing",int(v)/100.0),
                            bg="#222",fg="#ddd", troughcolor="#444",
                            highlightthickness=0)
        self.swing.set(int(state.swing*100)); self.swing.grid(row=1,column=4,sticky="w")

        # row2 Clock + MIDI device + Chan
        mid=tk.Frame(ctrl,bg="#222")
        mid.grid(row=2,column=0,columnspan=5,sticky="w",pady=4)

        tk.Label(mid,text="Clock",font=LF,fg="#ddd",bg="#222").pack(side="left")
        self.mode=tk.StringVar(value=state.clock_mode)
        tk.OptionMenu(mid,self.mode,"internal","send","receive",
                      command=lambda m:setattr(state,"clock_mode",m))\
          .pack(side="left",padx=(4,12))

        tk.Label(mid,text="Device",font=LF,fg="#ddd",bg="#222").pack(side="left")
        outs=self.be.midi_out.get_ports()
        self.port=tk.StringVar(value=outs[0] if outs else "MonomeSeq Out")
        tk.OptionMenu(mid,self.port,*outs,command=self.be.set_port)\
          .pack(side="left",padx=4)

        tk.Label(mid,text="Chan",font=LF,fg="#ddd",bg="#222").pack(side="left",padx=(12,2))
        self.chan=tk.Spinbox(mid,from_=1,to=16,width=3,command=self._set_chan)
        self.chan.delete(0,"end"); self.chan.insert(0,state.cur.midi_chan+1)
        self.chan.pack(side="left")

        # row3 Track nav + mute
        tk.Button(ctrl,text="◀",font=BF,width=2,command=self.prev_track)\
          .grid(row=3,column=0,pady=4)
        self.track_lab=tk.Label(ctrl,text=state.cur.name,font=LF,fg="#ddd",bg="#222")
        self.track_lab.grid(row=3,column=1,columnspan=2)
        tk.Button(ctrl,text="▶",font=BF,width=2,command=self.next_track)\
          .grid(row=3,column=3)
        self.mute_var=tk.IntVar(value=0)
        tk.Checkbutton(ctrl,text="Mute",variable=self.mute_var,
                       command=self._toggle_mute,selectcolor="#222",
                       bg="#222",fg="#ddd").grid(row=3,column=4,sticky="w")

        self.draw_grid()

    # ---- grid draw ----
    def draw_grid(self):
        c = self.canvas; c.delete("all")
        tr = state.cur
        for y in range(ROWS):
            disp = ROWS-1-y
            for x in range(COLS):
                vel = tr.steps[y][x]
                pls = (x == tr.playcol)
                fill = "#FFEE00" if pls and vel else \
                       "#44AAFF" if vel == VEL_LOW else \
                       "#FF9900" if vel == VEL_MED else \
                       "#FF0000" if vel == VEL_HI else "#333"
                x0, y0 = x*CELL_SIZE, disp*CELL_SIZE
                c.create_oval(x0+3, y0+3, x0+CELL_SIZE-3, y0+CELL_SIZE-3,
                              fill=fill, outline="#222")

    # ---- mouse click ----
    def _click(self,ev):
        col=ev.x//CELL_SIZE; row=ROWS-1-ev.y//CELL_SIZE
        if 0<=col<COLS and 0<=row<ROWS:
            state.cur.steps[row][col]^=1
            self.draw_grid(); self.be.redraw_monome()

    # ---- control callbacks ----
    def _toggle(self):
        state.running=not state.running
        self.play.config(text="Stop" if state.running else "Play",
                         foreground="red" if state.running else "green")
    def _oct(self, semitones):
        tr = state.cur
        tr.transpose += semitones
        self.oct.config(text=f"Oct {tr.transpose//12:+d}")   
    def _set_chan(self):
        ch=max(1,min(16,int(self.chan.get())))-1
        state.cur.midi_chan=ch
    def _toggle_mute(self):
        state.cur.mute=bool(self.mute_var.get()); self.be.redraw_monome()

    # track nav
    def next_track(self):
        state.cur_idx=(state.cur_idx+1)%TRACKS; self._refresh_track_ui()
    def prev_track(self):
        state.cur_idx=(state.cur_idx-1)%TRACKS; self._refresh_track_ui()
    def _refresh_track_ui(self):
        self.track_lab.config(text=state.cur.name)
        self.chan.delete(0,"end"); self.chan.insert(0,state.cur.midi_chan+1)
        self.mute_var.set(1 if state.cur.mute else 0)
        self.oct.config(text=f"Oct {state.cur.transpose//12:+d}")
        self.draw_grid(); self.be.redraw_monome()

# ───────── main ────────────────────────────────────────────
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