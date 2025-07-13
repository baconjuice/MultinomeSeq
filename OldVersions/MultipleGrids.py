#!/usr/bin/env python3

import asyncio
import functools
import monome

class DualGrid:
    def __init__(self):
        self.grids = []  # list of monome.Grid objects
        self.offsets = {}  # grid.id → horizontal offset
        self.led_state = [[0] * 16 for _ in range(8)]  # virtual 8×16 LED matrix

    async def start(self):
        self.serialosc = monome.SerialOsc()
        self.serialosc.device_added_event.add_handler(self._on_device_added)
        await self.serialosc.connect()

    def _on_device_added(self, id, type_, port):
        print(f"connecting to {id} ({type_})")
        grid = monome.Grid()
        asyncio.create_task(self._setup_grid(grid, port))

    async def _setup_grid(self, grid, port):
        await grid.connect('127.0.0.1', port)

        # Wait until grid.id is populated
        while grid.id is None:
            await asyncio.sleep(0.01)

        self.grids.append(grid)  # Now len(self.grids) is correct
        offset = 0 if len(self.grids) == 1 else 8
        self.offsets[grid.id] = offset

        grid.key_event.add_handler(functools.partial(self._on_key, grid))
        print(f"connected: {grid.id} at offset {offset}")

    def _on_key(self, grid, x, y, s):
        vx = x + self.offsets[grid.id]  # map to virtual x
        print(f"key: ({vx}, {y}) = {s}")
        if s:
            self.led_state[y][vx] ^= 15  # toggle between 0 and 15
            self._redraw()

    def _redraw(self):
        for grid in self.grids:
            offset = self.offsets[grid.id]
            for y in range(8):
                row_slice = self.led_state[y][offset:offset+8]
                grid.led_level_row(0, y, row_slice)

async def main():
    app = DualGrid()
    await app.start()
    await asyncio.Future()  # Keep the event loop alive

if __name__ == '__main__':
    asyncio.run(main())
