import tkinter as tk

CELL_SIZE = 20
ROWS = 8
COLS = 16

class SequencerGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Monome Sequencer GUI")

        # sequence state
        self.steps = [[0 for _ in range(COLS)] for _ in range(ROWS)]
        self.playcol = 0
        self.running = True
        self.octave_shift = 0
        self.bpm = 120

        # canvas for grid
        self.canvas = tk.Canvas(
            root,
            width=COLS * CELL_SIZE,
            height=ROWS * CELL_SIZE,
            bg='black'
        )
        self.canvas.pack()
        self.canvas.bind("<Button-1>", self.on_click)

        # controls
        self.controls = tk.Frame(root)
        self.controls.pack(pady=10)

        # BPM slider
        self.bpm_slider = tk.Scale(
            self.controls, from_=20, to=300, orient="horizontal",
            label="BPM", command=self.set_bpm
        )
        self.bpm_slider.set(self.bpm)
        self.bpm_slider.grid(row=0, column=0, padx=5)

        # Play/Stop button
        self.play_button = tk.Button(
            self.controls, text="Stop", command=self.toggle_play
        )
        self.play_button.grid(row=0, column=1, padx=5)

        # Octave shift
        self.octave_label = tk.Label(
            self.controls, text="Octave: +0"
        )
        self.octave_label.grid(row=0, column=2, padx=5)

        self.oct_up = tk.Button(
            self.controls, text="▲", command=self.shift_octave_up
        )
        self.oct_up.grid(row=0, column=3, padx=(5,0))

        self.oct_down = tk.Button(
            self.controls, text="▼", command=self.shift_octave_down
        )
        self.oct_down.grid(row=0, column=4)

        # initialize
        self.draw_grid()
        self.advance_playhead()

    def set_bpm(self, value):
        self.bpm = int(value)

    def toggle_play(self):
        self.running = not self.running
        self.play_button.config(text="Stop" if self.running else "Play")

    def shift_octave_up(self):
        self.octave_shift += 12
        self.octave_label.config(text=f"Octave: +{self.octave_shift//12}")

    def shift_octave_down(self):
        self.octave_shift -= 12
        self.octave_label.config(text=f"Octave: +{self.octave_shift//12}")

    def draw_grid(self):
        self.canvas.delete("all")
        for y in range(ROWS):
            for x in range(COLS):
                state = self.steps[y][x]
                row_display = ROWS - 1 - y

                if x == self.playcol:
                    fill = "Orange" if state else "Orange"
                else:
                    fill = "white" if state else "gray20"

                self.canvas.create_rectangle(
                    x * CELL_SIZE, row_display * CELL_SIZE,
                    (x + 1) * CELL_SIZE, (row_display + 1) * CELL_SIZE,
                    fill=fill, outline="gray50"
                )

    def on_click(self, event):
        col = event.x // CELL_SIZE
        row_display = event.y // CELL_SIZE
        y = ROWS - 1 - row_display
        if 0 <= col < COLS and 0 <= y < ROWS:
            self.steps[y][col] ^= 1
            self.draw_grid()

    def advance_playhead(self):
        if self.running:
            self.playcol = (self.playcol + 1) % COLS
            self.draw_grid()
        step_time_ms = int((60 / self.bpm / 4) * 1000)  # 16th note
        self.root.after(step_time_ms, self.advance_playhead)

# ---------- launch ----------
if __name__ == "__main__":
    root = tk.Tk()
    gui = SequencerGUI(root)
    root.mainloop()
