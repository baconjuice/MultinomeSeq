# MultinomeSeq

A multi-track step sequencer for Multiple Monome grids with MIDI output, GUI visualization, and per-step velocity control.

## Features
- **6 parallel tracks (pages)** with independent playheads, MIDI channels, and octaves.
- **MIDI output** via [RtMidi](https://github.com/thestk/rtmidi).
- **Clock modes**: Internal, Send, Receive (sync with external MIDI clock).
- **Per-step velocity levels** (low, medium, high).
- **Tkinter GUI** with real-time grid visualization and controls.
- **Multiple Monome support** (e.g., two 8x8 devices combined into 8x16).
- **Swing control** and **BPM slider**.
- **Track mute**, **octave transpose**, and **MIDI device selection**.
- **Starting key and **Scale selection

## Requirements
- Python 3.9+
- [monome](https://github.com/monome/serialosc.py) (`pip install monome`)
- [rtmidi-python](https://pypi.org/project/python-rtmidi/) (`pip install python-rtmidi`)

## Installation
```bash
git clone https://github.com/yourusername/MultinomeSeq.git
cd MultinomeSeq
pip install -r requirements.txt
```

## Usage
```bash
python MultinomeSeqV2.4.py
```

- Use your Monome grid(s) to toggle steps.
- Press a grid button multiple times to cycle through velocity levels (40, 80, 127).
- Use the GUI to control Clock source, BPM, swing, tracks, octave, key, scale and MIDI devices.

## Controls
- **GUI**:
  - **Play/Stop**: Start or pause the sequencer.
  - **Reset**: Start all patterns from the begining.
  - **Subdivision**: Change how the beat is subdivided, per page
  - **BPM Slider**: Set the tempo.
  - **Swing Slider**: Adjust swing feel.
  - **Octave Buttons**: Transpose the current track.
  - **Track Navigation**: Switch between the 6 tracks.
  - **Mute**: Temporarily mute a track.
  - **Midi In/Sync**: Select MIDI input port and Clock Options (Internal, Send. Receive)
  - **MIDI Device Dropdown**: Select available MIDI outputs, per track
  - **Root, Octave and Scale**: Select the starting note of the grid, the scale type and octave
  - **Load/Save**: Save/Loade your patterns and configuration
- **Monome**:
  - Tap a pad to toggle a step (cycles velocity - Low, Med, High and Off).
  - Long press to clear a step.

## Project Structure
- `MultinomeSeqV2.4.py` – Main application script.
- `README.md` – Project overview and instructions.
- `requirements.txt` – Python dependencies.

## License
- GNU GENERAL PUBLIC LICENSE
