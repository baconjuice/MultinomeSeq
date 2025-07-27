#!/usr/bin/env python3
# multinome_seq_tracks_velocity.py  –  4-track Monome sequencer (per-step velocity)

import asyncio, functools, contextlib, queue, threading, json
import tkinter as tk
from tkinter import filedialog
import monome, rtmidi

# Compatibility layer for different rtmidi versions
try:
    # Newer version (python-rtmidi >= 1.5.0)
    MidiOut = rtmidi.RtMidiOut
    MidiIn = rtmidi.RtMidiIn
except AttributeError:
    # Older version (python-rtmidi < 1.5.0)
    MidiOut = rtmidi.MidiOut
    MidiIn = rtmidi.MidiIn

# ───────── constants ─────────────────────────────────────
ROWS        = 8
CELL_SIZE   = 20
BASE_NOTE   = 36
GATE_RATIO  = 0.9
TRACKS      = 6
VEL_DEF     = 100          # velocity set by normal click
VEL_INC     = 15           # velocity increase on shift-click
# The main clock ticks once per 16th note. These values are multiples of that base tick.
SUBDIVISIONS = {
    "1/16": 1,
    "1/8": 2,
    "1/4": 4,
    "1/2": 8,
    "1/1": 16,
}

# ───────── scales ──────────────────────────────────────
SCALES = {
    "Chromatic": list(range(12)),
    "Major": [0, 2, 4, 5, 7, 9, 11],
    "Minor": [0, 2, 3, 5, 7, 8, 10],
    "Dorian": [0, 2, 3, 5, 7, 9, 10],
    "Phrygian": [0, 1, 3, 5, 7, 8, 10],
    "Lydian": [0, 2, 4, 6, 7, 9, 11],
    "Mixolydian": [0, 2, 4, 5, 7, 9, 10],
    "Locrian": [0, 1, 3, 5, 6, 8, 10],
    "Minor Pentatonic": [0, 3, 5, 7, 10],
    "Major Pentatonic": [0, 2, 4, 7, 9],
}
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
# ─────────────────────────────────────────────────────────

# ───────── data classes ─────────────────────────────────
class Track:
    def __init__(self, name, midi_chan=0, cols=0):
        self.name       = name
        self.steps      = [[0]*cols for _ in range(ROWS)]   # 0 = off, 1-127 = velocity
        self.playcol    = 0
        self.midi_chan  = midi_chan
        self.midi_out_port = "MonomeSeq Out"  # Default MIDI output port name
        self.mute       = False
        self.scale      = "Major"
        self.root_note  = 60  # C4
        self.subdivision = 1  # Pulses per step (default: 16th note = 1 pulse)

class SeqState:
    def __init__(self):
        self.cols       = 0
        self.tracks     = [Track(f"Track{i+1}", i, self.cols) for i in range(TRACKS)]
        self.cur_idx    = 0
        self.running    = True
        self.bpm        = 120
        self.swing      = 0.0
        self.midi_in_chan = 0           # 0 = All, 1-16
        self.clock_mode = "internal"   # internal | send | receive
        self.tick_count = 0            # For external MIDI clock
        self.beat_counter = 0          # For swing calculation

    @property
    def cur(self): return self.tracks[self.cur_idx]

    def resize_tracks(self, new_cols):
        """Resizes the step matrix for all tracks."""
        self.cols = new_cols
        for track in self.tracks:
            # Clamp playcol to new bounds before resizing steps
            if new_cols > 0:
                track.playcol %= new_cols
            else:
                track.playcol = 0

            old_cols = len(track.steps[0]) if track.steps and track.steps[0] else 0
            if new_cols == old_cols:
                continue

            new_steps = [[0] * new_cols for _ in range(ROWS)]
            for r in range(ROWS):
                for c in range(min(old_cols, new_cols)):
                    new_steps[r][c] = track.steps[r][c]
            track.steps = new_steps

state = SeqState()
# ─────────────────────────────────────────────────────────

# ───────── backend (Monome + threaded MIDI) ─────────────
class Backend:
    def _get_port_names(self, midi_obj):
        """Helper to get port names from rtmidi object."""
        try:
            # Newer API
            return [midi_obj.getPortName(i) for i in range(midi_obj.getPortCount())]
        except AttributeError:
            # Older API
            return midi_obj.get_ports()
    
    def _open_port(self, midi_obj, port_num):
        """Helper to open a port with API compatibility."""
        try:
            return midi_obj.openPort(port_num)
        except AttributeError:
            return midi_obj.open_port(port_num)
    
    def _open_virtual_port(self, midi_obj, name):
        """Helper to open virtual port with API compatibility."""
        try:
            return midi_obj.openVirtualPort(name)
        except AttributeError:
            return midi_obj.open_virtual_port(name)
    
    def _open_port_or_virtual(self, midi_obj, ports, port_num, virtual_name):
        """Helper to open port or virtual port."""
        if ports:
            self._open_port(midi_obj, port_num)
        else:
            self._open_virtual_port(midi_obj, virtual_name)
    
    def _close_port(self, midi_obj):
        """Helper to close port with API compatibility."""
        try:
            return midi_obj.closePort()
        except AttributeError:
            return midi_obj.close_port()
    
    def _send_message(self, midi_obj, msg):
        """Helper to send message with API compatibility."""
        try:
            return midi_obj.sendMessage(msg)
        except AttributeError:
            return midi_obj.send_message(msg)
    
    def _ignore_types(self, midi_obj, sysex, time, active_sense):
        """Helper to ignore types with API compatibility."""
        try:
            return midi_obj.ignoreTypes(sysex, time, active_sense)
        except AttributeError:
            return midi_obj.ignore_types(sysex, time, active_sense)
    
    def _set_callback(self, midi_obj, callback):
        """Helper to set callback with API compatibility."""
        try:
            return midi_obj.setCallback(callback)
        except AttributeError:
            return midi_obj.set_callback(callback)
    
    def __init__(self, loop):
        self.loop = loop
        # threaded MIDI-out - maintain backward compatibility
        self.midi_out = MidiOut()
        outs = self._get_port_names(self.midi_out)
        self._open_port_or_virtual(self.midi_out, outs, 0, "MonomeSeq Out")
        
        # Track multiple MIDI output devices for per-track routing
        self.midi_outputs = {}  # port_name -> MidiOut object
        self.midi_outputs["MonomeSeq Out"] = self.midi_out  # Default port
        
        self.midi_q   = queue.Queue()
        threading.Thread(target=self._midi_worker, daemon=True).start()

        # MIDI-in for external clock
        self.midi_in = None
        self.midi_clock_pending = False  # Flag for pending clock ticks
        self._setup_midi_input()
        
    def _setup_midi_input(self):
        """Setup MIDI input with proper error handling."""
        self.midi_callback_active = False
        
        try:
            self.midi_in = MidiIn()
            self._ignore_types(self.midi_in, False, False, False)  # Don't ignore timing messages
            
            ins = self._get_port_names(self.midi_in)
            print(f"Available MIDI input ports: {ins}")
            
            if ins: 
                print("✓ MIDI input ready - use GUI to select port")
                self.midi_callback_active = True  # Enable callback mode
            else:
                print("⚠ No MIDI input ports available")
                
        except Exception as e:
            print(f"✗ Failed to setup MIDI input: {e}")
            self.midi_in = None
            self.midi_callback_active = False

        # Monome
        self.grid_map, self.offsets, self.gui = {}, {}, None
        self.press_times, self.running = {}, True  # track key press timestamps
        self.grid_lock = asyncio.Lock()

    # threaded sender
    def _midi_worker(self):
        while True:
            msg = self.midi_q.get()
            if msg is None: break
            
            # Check if message includes port specification
            if isinstance(msg, tuple) and len(msg) == 2:
                port_name, midi_data = msg
                midi_out = self.get_midi_output(port_name)
                with contextlib.suppress(Exception):
                    self._send_message(midi_out, midi_data)
            else:
                # Default behavior - use main MIDI output
                with contextlib.suppress(Exception):
                    self._send_message(self.midi_out, msg)

    def qmsg(self, *b): 
        self.midi_q.put(list(b))
    
    def qmsg_to_port(self, port_name, *b):
        """Send MIDI message to specific port."""
        self.midi_q.put((port_name, list(b)))
    
    def get_midi_output(self, port_name):
        """Get or create MIDI output for specific port."""
        if port_name not in self.midi_outputs:
            try:
                midi_out = MidiOut()
                outs = self._get_port_names(midi_out)
                
                # Try to open the specific port
                for i, p in enumerate(outs):
                    if p == port_name:
                        self._open_port(midi_out, i)
                        self.midi_outputs[port_name] = midi_out
                        return midi_out
                
                # If port not found, create virtual port
                self._open_virtual_port(midi_out, f"{port_name} (virtual)")
                self.midi_outputs[port_name] = midi_out
                return midi_out
                
            except Exception:
                # Fallback to default output
                return self.midi_out
        
        return self.midi_outputs[port_name]

    def set_port(self,name:str):
        with contextlib.suppress(Exception): self._close_port(self.midi_out)
        self.midi_out = MidiOut()
        outs = self._get_port_names(self.midi_out)
        for i,p in enumerate(outs):
            if p==name: self._open_port(self.midi_out, i); return
        self._open_virtual_port(self.midi_out, "MonomeSeq Out (virtual)")

    def set_in_port(self, name: str):
        """Switch to a different MIDI input port."""
        
        # Close current port if open
        if self.midi_in and hasattr(self.midi_in, 'is_port_open') and self.midi_in.is_port_open():
            with contextlib.suppress(Exception): 
                self._close_port(self.midi_in)
        
        # Find and open the new port
        ins = self._get_port_names(self.midi_in)
        for i, p in enumerate(ins):
            if p == name: 
                try:
                    self._open_port(self.midi_in, i)
                    print(f"✓ MIDI input: {name}")
                    
                    # Try to set callback on new port
                    if self.midi_callback_active:
                        try:
                            self._set_callback(self.midi_in, self._clock_in)
                        except Exception as e:
                            print(f"⚠ MIDI callback failed: {e}")
                    
                    return
                except Exception as e:
                    print(f"✗ Failed to open MIDI port {name}: {e}")
        
        print(f"✗ MIDI port {name} not found")

    # SerialOSC
    async def start(self) -> None:
        asyncio.create_task(self._serialosc())
        threading.Thread(target=self._threaded_clock_loop, daemon=True).start()

    async def _serialosc(self):
        try:
            s = monome.SerialOsc()
            s.device_added_event.add_handler(self._grid_added)
            s.device_removed_event.add_handler(self._grid_removed)
            await s.connect()
        except Exception as e:
            print(f"Error connecting to serialosc: {e}")

    def _grid_added(self,i,t,port):
        g=monome.Grid(); asyncio.create_task(self._setup_grid(g,port))

    def _grid_removed(self, id_, type_, port):
        """Schedules the asynchronous removal of a grid."""
        asyncio.create_task(self._remove_grid_async(id_))

    async def _remove_grid_async(self, id_):
        """Handles Mono encer."""
        async with self.grid_lock:
            print(f"Disconnection event received for Monome ID: '{id_}'")    
            # The primary source of truth for a device being tracked is its offset.
            # If we don't have an offset, it has already been fully removed.
            if id_ not in self.offsets:
                print(f"Monome '{id_}' not found in tracked offsets. Assuming already processed.")
                return
            print(f"Monome '{id_}' found. Proceeding with removal and resize...")    
            grid_to_remove = self.grid_map.get(id_)

            # --- 1. Attempt to cleanly disconnect the hardware ---
            if grid_to_remove:
                # Use the id_ from the event, which is reliable.
                # The grid_to_remove.id might have been nulled by the library already.
                print(f"Attempting to clear and disconnect '{id_}'...")
                with contextlib.suppress(Exception): grid_to_remove.led_all(0)
                await asyncio.sleep(0.1)
                grid_to_remove.disconnect()
                print(f"Disconnection command sent for '{id_}'.")
            else:
                print(f"Warning: No grid object in map for '{id_}'. Proceeding with state cleanup.")

            # --- 2. Rebuild state from the remaining grids ---
            # Create a new list of grids to keep, excluding the one being removed.
            grids_to_keep = [g for g in self.grid_map.values() if g.id != id_]

            if not grids_to_keep:
                self.grid_map, self.offsets = {}, {}
                state.resize_tracks(0)

            # Use the old offsets dict to sort the remaining grids, ensuring order is preserved.
            sorted_grids = sorted(grids_to_keep, key=lambda g: self.offsets.get(g.id, 0))
    
            # Recalculate all offsets and total width from scratch.
            new_total_cols, new_offsets, new_grid_map = 0, {}, {}
            for g in sorted_grids:
                if g.width is not None:
                    new_offsets[g.id] = new_total_cols
                    new_grid_map[g.id] = g
                    new_total_cols += g.width

            # --- 3. Atomatically update the application state ---
            # This replaces the old state entirely, ensuring stale entries are removed.
            self.grid_map, self.offsets = new_grid_map, new_offsets
            state.resize_tracks(new_total_cols)
            print(f"Sequencer resized to {new_total_cols} columns.")

            # --- 4. Update GUI and remaining hardware ---
            if self.gui:
                self.gui.resize_canvas(new_total_cols)
                self.gui._refresh_ui()
            self.redraw_monome()

    async def _setup_grid(self,g,port):
        """Adds a new grid, resizing the sequencer and GUI."""
        async with self.grid_lock:
            start_time = asyncio.get_event_loop().time()
            await g.connect("127.0.0.1",port)
            while g.id is None or g.width is None:
                await asyncio.sleep(0.01)

            connect_duration = asyncio.get_event_loop().time() - start_time
            print(f"Initial connection for '{g.id}' established in {connect_duration:.4f} seconds.")

            # Prevent adding a grid that is already being tracked. If a duplicate
            # event comes in, we must disconnect the new grid object to prevent leaks.
            if g.id in self.offsets:
                print(f"Monome '{g.id}' is already connected. Ignoring duplicate add event.")
                with contextlib.suppress(Exception):
                    g.disconnect()
                return

            # --- 1. Stabilize the connection BEFORE doing anything else. ---
            # This seems to be the crucial step for m64 hardware.
            print(f"Stabilizing hardware link for '{g.id}'...")
            await asyncio.sleep(0.5)

            # --- 2. Handshake: Test the OUTPUT channel first. ---
            try:
                print(f"Testing hardware link for '{g.id}' with LED flash...")
                g.led_all(1)
                await asyncio.sleep(1)
                g.led_all(0)
                print(f"Output to '{g.id}' appears stable.")
            except Exception as e:
                print(f"Error during hardware handshake with '{g.id}': {e}")
                print("Aborting setup for this device.")
                with contextlib.suppress(Exception):
                    g.disconnect()
                return

            # --- 3. Now subscribe to the INPUT channel. ---
            g.key_event.add_handler(functools.partial(self._on_key,g))
            print(f"Key handler registered for '{g.id}'.")

            # --- 4. Now that hardware is confirmed responsive, update all internal state ---
            print(f"Monome '{g.id}' ({g.width}x{g.height}) connected. Updating state.")
            current_cols = state.cols
            new_total_cols = current_cols + g.width

            self.offsets[g.id] = current_cols
            self.grid_map[g.id] = g

            state.resize_tracks(new_total_cols)
            print(f"Sequencer resized to {new_total_cols} columns.")

            # --- 5. Update the GUI ---
            if self.gui:
                self.gui.resize_canvas(new_total_cols)
                self.gui._refresh_ui()

            # --- 6. Final hardware redraw ---
            self.redraw_monome()
            total_duration = asyncio.get_event_loop().time() - start_time
            print(f"Hardware for '{g.id}' fully initialized in {total_duration:.4f} seconds.")

    # key handler (velocity toggle / increment)
    def _on_key(self, g, x, y, s):
        if not (0 <= x < g.width and 0 <= y < g.height): return
        vx = x + self.offsets[g.id]
        vy = ROWS - 1 - y
        cur = state.cur
        key = (g.id, vx, vy)

        if s:  # key down
            self.press_times[key] = asyncio.get_event_loop().time()
        else:  # key up
            start_time = self.press_times.pop(key, None)
            if start_time is None:
                return
            duration = asyncio.get_event_loop().time() - start_time

            vel = cur.steps[vy][vx]
            if duration >= 0.5:
                # Long press → clear step
                cur.steps[vy][vx] = 0
            else:
                # Short press → cycle velocity
                if vel == 0:
                    cur.steps[vy][vx] = 40
                elif vel == 40:
                    cur.steps[vy][vx] = 80
                elif vel == 80:
                    cur.steps[vy][vx] = 127
                else:
                    cur.steps[vy][vx] = 0

            if self.gui and self.gui.canvas.winfo_exists():
                try:
                    self.gui.draw_grid()
                except tk.TclError as e:
                    print(f"GUI Error during draw_grid: {e}")
            self.redraw_monome()

    # LED redraw (steps of current track, playheads all)
    def redraw_monome(self):
        for g in self.grid_map.values(): # Iterate over the map's values
            # Defensive check in case a grid disconnects or its ID is not yet registered
            if g.id is None or g.id not in self.offsets:
                continue
            off=self.offsets[g.id]
            for y in range(ROWS):
                row=[1 if state.cur.steps[y][x]>0 else 0 for x in range(off, off + g.width)]
                # Overlay the playhead for the current track only
                tr = state.cur
                if not tr.mute and off <= tr.playcol < off + g.width:
                    row[tr.playcol-off]=1
                g.led_row(0, ROWS-1-y, row)

    
    # MIDI clock IN - ultra-lightweight callback
    def _clock_in(self, event, _):
        # Skip processing if not in receive mode
        if state.clock_mode != "receive": 
            return
        
        try:
            msg, _ = event
            if not msg:
                return
                
            b = msg[0]
            
            # Process MIDI messages with minimal overhead
            if b == 0xFA:  # MIDI Start
                state.running = True
                state.tick_count = 0
                state.beat_counter = 0
                for track in state.tracks:
                    track.playcol = 0
                print("MIDI Start")
            elif b == 0xFB:  # MIDI Continue
                state.running = True
                print("MIDI Continue")
            elif b == 0xFC:  # MIDI Stop
                state.running = False
                print("MIDI Stop")
            elif b == 0xF8 and state.running:  # MIDI Clock - minimal processing
                state.tick_count = (state.tick_count + 1) % 6
                if state.tick_count == 0:
                    self.midi_clock_pending = True
        except:
            # Silently ignore callback errors to prevent timing issues
            pass

    # This clock runs in a separate thread to ensure its timing is not
    # affected by GUI workload or other asyncio tasks.
    def _threaded_clock_loop(self):
        import time
        while self.running:
            # Poll for MIDI messages in receive mode (only if callback is not active)
            if state.clock_mode == "receive" and self.midi_in and not self.midi_callback_active:
                try:
                    # Check for MIDI messages (polling)
                    msg = self.midi_in.get_message()
                    if msg and msg[0]:  # get_message returns (message, timestamp)
                        self._process_midi_message(msg[0])
                except Exception as e:
                    pass
            
            # Handle pending MIDI clock ticks
            if self.midi_clock_pending and state.clock_mode == "receive":
                asyncio.run_coroutine_threadsafe(self._step(), self.loop)
                self.midi_clock_pending = False
            
            if state.running and state.clock_mode in ("internal","send"):
                # Safely schedule the _step coroutine to run on the main asyncio loop
                asyncio.run_coroutine_threadsafe(self._step(), self.loop)
                if state.clock_mode=="send":
                    for _ in range(6): self.qmsg(0xF8)
            step=60/state.bpm/4
            sw=state.swing
            delay=step*(1+sw) if state.beat_counter%2 else step*(1-sw)
            time.sleep(delay)
    
    def _process_midi_message(self, msg):
        """Process a MIDI message (for polling mode)."""
        if not msg or state.clock_mode != "receive":
            return
            
        b = msg[0]
        
        if b == 0xFA:  # MIDI Start
            state.running = True
            state.tick_count = 0
            state.beat_counter = 0
            for track in state.tracks:
                track.playcol = 0
            print("MIDI Start")
        elif b == 0xFB:  # MIDI Continue
            state.running = True
            print("MIDI Continue")
        elif b == 0xFC:  # MIDI Stop
            state.running = False
            print("MIDI Stop")
        elif b == 0xF8 and state.running:  # MIDI Clock
            state.tick_count = (state.tick_count + 1) % 6
            if state.tick_count == 0:
                self.midi_clock_pending = True

    # step
    async def _step(self):
        # If no grids are connected, sequencer has 0 columns. Do nothing.
        if state.cols == 0:
            return

        did_play = False
        for tr in state.tracks:
            if tr.mute:
                continue

            if (state.beat_counter % tr.subdivision) == 0:
                did_play = True

                # Calculate current playhead position for this track
                tr.playcol = (state.beat_counter // tr.subdivision) % state.cols

                scale_intervals = SCALES.get(tr.scale, SCALES["Chromatic"])
                num_degrees = len(scale_intervals)

                notes = []
                for r in range(ROWS):
                    vel = tr.steps[r][tr.playcol]
                    if vel > 0:
                        octave = r // num_degrees
                        degree = r % num_degrees
                        note_offset = (octave * 12) + scale_intervals[degree]
                        note = tr.root_note + note_offset
                        self.qmsg_to_port(tr.midi_out_port, 0x90 | tr.midi_chan, note, vel)
                        notes.append(note)
                asyncio.create_task(self._note_off(notes, tr.midi_chan, tr.midi_out_port))

        if did_play:
            self.redraw_monome()
            if self.gui and self.gui.canvas.winfo_exists():
                try:
                    self.gui.draw_grid()
                except tk.TclError as e:
                    print(f"GUI Error during draw_grid: {e}")

        # Increment beat counter at the end of the step for all modes
        state.beat_counter += 1


    async def _note_off(self, ns, ch, midi_out_port="MonomeSeq Out"):
        await asyncio.sleep((60/state.bpm/4)*GATE_RATIO)
        for n in ns: self.qmsg_to_port(midi_out_port, 0x80|ch, n, 0)

    def shutdown(self):
        self.running = False # Set running flag to false
        with contextlib.suppress(Exception): self._close_port(self.midi_out)
        with contextlib.suppress(Exception): self._close_port(self.midi_in)

# ───────── GUI ────────────────────────────────────────────
class SequencerGUI:
    def __init__(self,root,be:Backend):
        self.be=be; be.gui=self
        root.configure(bg="#222"); root.title("Monome Seq Tracks (Velocity)")

        # Canvas starts at 0 width, will be resized when grids connect
        self.canvas=tk.Canvas(root,width=0,height=ROWS*CELL_SIZE,
                              bg="#222",highlightthickness=0)
        self.canvas.pack(padx=8,pady=(8,4), fill="x", expand=True)

        ctrl=tk.Frame(root,bg="#222"); ctrl.pack(padx=8,pady=6,anchor="w")
        LF,BF=("Helvetica",10),("Helvetica",10,"bold")

        # row0 BPM + Play + Reset
        tk.Label(ctrl,text="BPM",font=LF,fg="#ddd",bg="#222").grid(row=0,column=0,sticky="e")
        self.bpm=tk.Scale(ctrl,from_=40,to=300,orient="horizontal",length=90,
                          command=lambda v:setattr(state,"bpm",int(float(v))),
                          bg="#222",fg="#ddd",troughcolor="#444",highlightthickness=0)
        self.bpm.set(state.bpm); self.bpm.grid(row=0,column=1,columnspan=2,sticky="we")
        self.play=tk.Button(ctrl,text="Stop",width=6,font=BF,bg="green",fg="red",
                            command=self._toggle)
        self.play.grid(row=0, column=3, padx=4)
        tk.Button(ctrl, text="Reset", font=BF, width=6, command=self._reset_sequence)\
            .grid(row=0, column=4, padx=4)
        ctrl.grid_columnconfigure(1,weight=1)

        # row1 Swing + Subdivision + Clock
        swing_sub_frame = tk.Frame(ctrl, bg="#222")
        swing_sub_frame.grid(row=1, column=0, columnspan=5, sticky="w")

        tk.Label(swing_sub_frame, text="Swing", font=LF, fg="#ddd", bg="#222").pack(side="left")
        self.swing=tk.Scale(swing_sub_frame, from_=0, to=50, orient="horizontal", length=100,
                            command=lambda v:setattr(state,"swing",int(v)/100.0),
                            bg="#222",fg="#ddd",troughcolor="#444",highlightthickness=0)
        self.swing.set(int(state.swing*100))
        self.swing.pack(side="left", padx=(0, 12))

        tk.Label(swing_sub_frame, text="Subdivision", font=LF, fg="#ddd", bg="#222").pack(side="left")
        self.subdiv_var = tk.StringVar()
        tk.OptionMenu(swing_sub_frame, self.subdiv_var, *SUBDIVISIONS.keys(), command=self._set_subdivision).pack(side="left", padx=4)

        #row 2 Midi in/out
        mid_ports = tk.Frame(ctrl, bg="#222"); mid_ports.grid(row=2, column=0, columnspan=5, sticky="w", pady=4)

        # --- MIDI In Section ---
        in_frame = tk.LabelFrame(mid_ports, text="MIDI In / Sync", font=LF, fg="#ddd", bg="#222", bd=1, relief="groove")
        in_frame.pack(side="left", padx=2, pady=2, fill="y")

        # MIDI In Port
        tk.Label(in_frame, text="Port", font=LF, fg="#ddd", bg="#222").pack(side="left", padx=(5,2))
        self.midi_in_port_var = tk.StringVar()
        ins = self.be._get_port_names(self.be.midi_in)
        
        # Prefer IAC Driver Bus 1 if available, otherwise first port
        preferred_port = ins[0] if ins else "None"
        for port in ins:
            if "IAC Driver Bus 1" in port:
                preferred_port = port
                break
        
        self.midi_in_port_var.set(preferred_port)
        self.midi_in_menu = tk.OptionMenu(in_frame, self.midi_in_port_var, *ins, command=self._on_midi_in_port_change)
        self.midi_in_menu.config(width=15)
        self.midi_in_menu.pack(side="left", padx=4)
        
        # Set the initial port
        if ins:
            self.be.set_in_port(preferred_port)

        # MIDI In Channel
        tk.Label(in_frame, text="In Ch", font=LF, fg="#ddd", bg="#222").pack(side="left", padx=(8, 2))
        self.in_chan_spin = tk.Spinbox(in_frame, from_=0, to=16, width=3, command=self._set_chan) # 0 for All
        self.in_chan_spin.delete(0, "end"); self.in_chan_spin.insert(0, state.midi_in_chan)
        self.in_chan_spin.pack(side="left")

        # Clock Mode
        tk.Label(in_frame, text="Clock", font=LF, fg="#ddd", bg="#222").pack(side="left", padx=(8,2))
        self.mode = tk.StringVar(value=state.clock_mode)
        tk.OptionMenu(in_frame, self.mode, "internal", "send", "receive", command=lambda m: setattr(state, "clock_mode", m)).pack(side="left")

        # --- MIDI Out Section (Per-Track) ---
        out_frame = tk.LabelFrame(mid_ports, text="MIDI Out (Per Track)", font=LF, fg="#ddd", bg="#222", bd=1, relief="groove")
        out_frame.pack(side="left", padx=2, pady=2, fill="y")

        # MIDI Out Port (Per Track)
        tk.Label(out_frame, text="Port", font=LF, fg="#ddd", bg="#222").pack(side="left", padx=2)
        outs = self.be._get_port_names(self.be.midi_out)
        self.track_port = tk.StringVar(value=state.cur.midi_out_port)
        self.track_port_menu = tk.OptionMenu(out_frame, self.track_port, state.cur.midi_out_port, *outs, command=self._set_track_port)
        self.track_port_menu.config(width=15)
        self.track_port_menu.pack(side="left", padx=4)
        tk.Button(out_frame, text="Refresh", font=BF, command=self._refresh_midi_ports).pack(side="left", padx=4)
        tk.Label(out_frame, text="Ch", font=LF, fg="#ddd", bg="#222").pack(side="left", padx=(12, 2))
        self.chan = tk.Spinbox(out_frame, from_=1, to=16, width=3, command=self._set_chan)
        self.chan.delete(0, "end"); self.chan.insert(0, state.cur.midi_chan + 1)
        self.chan.pack(side="left")

        # row4 track nav + mute
        tk.Button(ctrl, text="◀", font=BF, width=2, command=self.prev_track).grid(row=3, column=0, pady=4)
        self.track_name_var = tk.StringVar()
        self.track_name_entry = tk.Entry(ctrl, textvariable=self.track_name_var, font=LF, bg="#555", fg="#ddd", width=30, justify='center', bd=0, highlightthickness=1, highlightbackground="#444")
        self.track_name_entry.grid(row=3, column=1, columnspan=2)
        self.track_name_entry.bind("<Return>", self._set_track_name)
        self.track_name_entry.bind("<FocusOut>", self._set_track_name)
        tk.Button(ctrl, text="▶", font=BF, width=2, command=self.next_track).grid(row=3, column=3)
        self.mute_var = tk.IntVar(value=0)
        tk.Checkbutton(ctrl, text="Mute", variable=self.mute_var, selectcolor="#222", command=self._toggle_mute, bg="#222", fg="#ddd").grid(row=3, column=4, sticky="w")

        # row5 Scale controls
        scale_frame = tk.Frame(ctrl, bg="#222")
        scale_frame.grid(row=5, column=0, columnspan=5, sticky="w", pady=4)

        tk.Label(scale_frame, text="Root", font=LF, fg="#ddd", bg="#222").pack(side="left")
        self.root_note_var = tk.StringVar()
        tk.OptionMenu(scale_frame, self.root_note_var, *NOTE_NAMES, command=self._set_root_note_name).pack(side="left", padx=2)

        self.root_oct_spin = tk.Spinbox(scale_frame, from_=-2, to=8, width=3, command=self._set_root_note_oct)
        self.root_oct_spin.pack(side="left")

        tk.Label(scale_frame, text="Scale", font=LF, fg="#ddd", bg="#222").pack(side="left", padx=(12, 0))
        self.scale_var = tk.StringVar()
        tk.OptionMenu(scale_frame, self.scale_var, *SCALES.keys(), command=self._set_scale).pack(side="left", padx=4)

        # row6 File ops
        file_ops = tk.Frame(ctrl, bg="#222")
        file_ops.grid(row=6, column=0, columnspan=5, sticky="w", pady=4)
        tk.Button(file_ops, text="Load", font=BF, command=self._load_pattern)\
          .pack(side="left", padx=4)
        tk.Button(file_ops, text="Save", font=BF, command=self._save_pattern)\
          .pack(side="left", padx=4)

        self._refresh_ui()

    def resize_canvas(self, new_cols):
        self.canvas.config(width=new_cols * CELL_SIZE)
        self.draw_grid()

    # ---- draw grid (3-level velocity shading) ----
    def draw_grid(self):
        c = self.canvas
        c.delete("all")
        tr = state.cur  # This was the missing line
        if state.cols == 0: return
        for y in range(ROWS):
            disp = ROWS - 1 - y
            for x in range(state.cols):
                vel = tr.steps[y][x]

                # colour by velocity
                if vel == 0:
                    fill = "#444"
                elif vel <= 40:
                    fill = "#3366FF"
                elif vel <= 80:
                    fill = "#33CC33"
                else:
                    fill = "#FFCC00"

                x0, y0 = x * CELL_SIZE, disp * CELL_SIZE
                c.create_oval(x0+3, y0+3, x0+CELL_SIZE-3, y0+CELL_SIZE-3,
                            fill=fill, outline="#333")

        # Draw the playhead for the current track only
        if not tr.mute:
            c.create_rectangle(
                tr.playcol * CELL_SIZE, 0,
                (tr.playcol + 1) * CELL_SIZE, ROWS * CELL_SIZE,
                outline="#F19225", width=2
            )

    # ---- mouse clicks ----
    def _click(self, ev):
        col = ev.x // CELL_SIZE
        row = ROWS - 1 - ev.y // CELL_SIZE
        if 0 <= col < state.cols and 0 <= row < ROWS:
            cur = state.cur
            vel = cur.steps[row][col]
            if vel == 0:
                cur.steps[row][col] = 40
            elif vel == 40:
                cur.steps[row][col] = 80
            elif vel == 80:
                cur.steps[row][col] = 127
            else:
                cur.steps[row][col] = 0
            self.draw_grid()
            self.be.redraw_monome()

    def _save_pattern(self):
        # First, ensure any pending edit in the track name entry is saved to the state.
        self._set_track_name()

        filepath = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            title="Save Pattern"
        )
        if not filepath: return

        data_to_save = {
            'cols': state.cols,
            'bpm': state.bpm, 'swing': state.swing, 'tracks': []
        }
        for track in state.tracks:
            data_to_save['tracks'].append({
                'name': track.name, 'steps': track.steps,
                'midi_chan': track.midi_chan,
                'midi_out_port': track.midi_out_port,
                'mute': track.mute, 'scale': track.scale,
                'root_note': track.root_note, 'subdivision': track.subdivision
            })
        try:
            with open(filepath, 'w') as f: json.dump(data_to_save, f, indent=2)
            print(f"Pattern saved to {filepath}")
        except IOError as e: print(f"Error saving file: {e}")

    def _load_pattern(self):
        filepath = filedialog.askopenfilename(
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            title="Load Pattern"
        )
        if not filepath: return

        try:
            with open(filepath, 'r') as f: loaded_data = json.load(f)
        except (IOError, json.JSONDecodeError) as e:
            print(f"Error loading file: {e}"); return

        state.bpm = loaded_data.get('bpm', 120)
        state.swing = loaded_data.get('swing', 0.0)
        for i, track_data in enumerate(loaded_data.get('tracks', [])):
            if i < len(state.tracks):
                for key, val in track_data.items():
                    if key == 'steps':
                        loaded_steps = val
                        new_steps = [[0] * state.cols for _ in range(ROWS)]
                        for r in range(ROWS):
                            for c in range(min(len(loaded_steps[r]), state.cols)):
                                new_steps[r][c] = loaded_steps[r][c]
                        state.tracks[i].steps = new_steps
                    elif hasattr(state.tracks[i], key):
                        setattr(state.tracks[i], key, val)
                
                # Ensure midi_out_port is set (backward compatibility)
                if not hasattr(state.tracks[i], 'midi_out_port'):
                    state.tracks[i].midi_out_port = "MonomeSeq Out"
        self.bpm.set(state.bpm)
        self.swing.set(state.swing * 100)
        self._refresh_ui()

    def _on_midi_in_port_change(self, selected_port):
        """Called when user selects a different MIDI input port."""
        self.be.set_in_port(selected_port)
    
    def _refresh_midi_ports(self):
        outs = self.be._get_port_names(self.be.midi_out)
        if outs:
            # Update track-specific MIDI output port menu
            menu = self.track_port_menu.children["menu"]
            menu.delete(0, "end")  # Clear existing options
            for port_name in outs:
                menu.add_command(label=port_name, command=tk._setit(self.track_port, port_name, self._set_track_port))
            # Only set to first port if no port is currently selected
            if not self.track_port.get() or self.track_port.get() == "No devices found":
                self.track_port.set(outs[0])
                state.cur.midi_out_port = outs[0]
        else:
            self.track_port.set("No devices found")
        self.refresh_midi_in_ports()

    def _set_track_name(self, event=None):
        """Saves the edited track name when Enter is pressed or focus is lost."""
        new_name = self.track_name_var.get().strip()
        if new_name:
            state.cur.name = new_name
        else:
            # If user clears the name, revert to the current name
            self.track_name_var.set(state.cur.name)
        self.track_name_entry.master.focus_set() # Unfocus the entry

    def _set_root_note_name(self, note_name):
        """Sets the scale's root note, preserving the octave."""
        current_octave = state.cur.root_note // 12
        note_index = NOTE_NAMES.index(note_name)
        state.cur.root_note = (current_octave * 12) + note_index

    def _set_root_note_oct(self):
        """Sets the octave for the scale's root note."""
        new_octave = int(self.root_oct_spin.get())
        note_in_octave = state.cur.root_note % 12
        state.cur.root_note = (new_octave + 1) * 12 + note_in_octave

    def _set_scale(self, scale_name):
        """Sets the musical scale for the current track."""
        state.cur.scale = scale_name

    def _set_subdivision(self, subdiv_name):
        """Sets the clock subdivision for the current track."""
        state.cur.subdivision = SUBDIVISIONS.get(subdiv_name, 1)

    def _reset_sequence(self):
        """Resets all track playheads and counters to the beginning."""
        print("Resetting sequence to start.")
        state.beat_counter = 0
        for track in state.tracks:
            track.playcol = 0
        self.draw_grid()
        self.be.redraw_monome()

    # ---- control callbacks ----
    def _toggle(self):
        state.running=not state.running
        self.play.config(text="Stop" if state.running else "Play",
                         fg="red" if state.running else "green")
    def _set_chan(self):
        ch=max(1,min(16,int(self.chan.get())))-1
        state.cur.midi_chan=ch
    
    def _set_track_port(self, port_name):
        """Set MIDI output port for current track."""
        state.cur.midi_out_port = port_name
        # Ensure the port is available in backend
        self.be.get_midi_output(port_name)
    def _toggle_mute(self):
        state.cur.mute=bool(self.mute_var.get()); self.be.redraw_monome()

    # track navigation
    def next_track(self):
        state.cur_idx=(state.cur_idx+1)%TRACKS
        self._refresh_ui()
        self.be.redraw_monome()

    def prev_track(self):
        state.cur_idx=(state.cur_idx-1)%TRACKS
        self._refresh_ui()
        self.be.redraw_monome()

    def _refresh_ui(self):
        """Updates all GUI elements for the current track, but does NOT touch hardware."""
        # Update track name and channel
        self.track_name_var.set(state.cur.name)
        self.track_name_entry.config(highlightbackground="#F19225")  # Highlight current track
        self.chan.delete(0,"end"); self.chan.insert(0,state.cur.midi_chan+1)
        
        # Update MIDI output port
        self.track_port.set(state.cur.midi_out_port)
        
        # Update mute state
        self.mute_var.set(1 if state.cur.mute else 0)

        # Update scale controls
        root_note = state.cur.root_note
        octave = (root_note // 12) - 1
        note_name = NOTE_NAMES[root_note % 12]
        self.root_note_var.set(note_name)
        self.root_oct_spin.delete(0, "end"); self.root_oct_spin.insert(0, str(octave))
        self.scale_var.set(state.cur.scale)

        # Update subdivision control
        subdiv_name = next((k for k, v in SUBDIVISIONS.items() if v == state.cur.subdivision), "1/16")
        self.subdiv_var.set(subdiv_name)

        self.draw_grid()

    def refresh_midi_in_ports(self):
        """Refresh MIDI input port options without changing current selection."""
        ports = self.be._get_port_names(self.be.midi_in)
        if ports:
            # Update the menu options
            menu = self.midi_in_menu.children["menu"]
            menu.delete(0, "end")  # Clear existing options
            for port_name in ports:
                menu.add_command(label=port_name, command=tk._setit(self.midi_in_port_var, port_name))
            # Only set to first port if no port is currently selected
            if not self.midi_in_port_var.get() or self.midi_in_port_var.get() == "No devices found":
                self.midi_in_port_var.set(ports[0])
        else:
            self.midi_in_port_var.set("No devices found")

# ───────── main entry ─────────────────────────────────────
async def main():
  root = tk.Tk()
  loop = asyncio.get_running_loop()
  be = Backend(loop)
  SequencerGUI(root, be)
  await be.start() # This now starts the background threads

  def on_close():  # Define a function to handle window close
      be.shutdown()  # Signal backend tasks to stop
      root.destroy()  # Destroy the Tkinter root window

  root.protocol("WM_DELETE_WINDOW", on_close)  # Register the close handler

  try:
      while be.running:  # Run as long as the backend is running
          root.update()
          await asyncio.sleep(0.01)
  except (tk.TclError, RuntimeError):
      # This can happen if the window is closed abruptly.
      pass

if __name__=="__main__":
    asyncio.run(main())
