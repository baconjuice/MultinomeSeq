#!/usr/bin/env python3
import rtmidi
import time

# Test MIDI input to see what ports are available and what messages are received
try:
    midi_in = rtmidi.MidiIn()
    
    # List all available ports
    print("Available MIDI Input Ports:")
    try:
        ports = [midi_in.getPortName(i) for i in range(midi_in.getPortCount())]
    except AttributeError:
        ports = midi_in.get_ports()
    
    for i, port in enumerate(ports):
        print(f"  {i}: {port}")
    
    if not ports:
        print("No MIDI ports available!")
        exit(1)
    
    # Ask user which port to use
    print(f"\nWhich port should we listen to? (0-{len(ports)-1}): ")
    try:
        port_num = int(input())
        if port_num < 0 or port_num >= len(ports):
            print("Invalid port number!")
            exit(1)
    except ValueError:
        print("Invalid input!")
        exit(1)
    
    # Set up callback
    def debug_callback(event, data):
        msg, timestamp = event
        if msg:
            print(f"MIDI: {[hex(x) for x in msg]} (port: {ports[port_num]})")
    
    # Open port and set callback
    try:
        midi_in.openPort(port_num)
    except AttributeError:
        midi_in.open_port(port_num)
    
    midi_in.ignore_types(False, False, False)  # Don't ignore any messages
    
    try:
        midi_in.set_callback(debug_callback)
    except AttributeError:
        midi_in.setCallback(debug_callback)
    
    print(f"\nListening on port {port_num}: {ports[port_num]}")
    print("Start playback in Ableton Live...")
    print("Press Ctrl+C to stop\n")
    
    # Listen for MIDI
    while True:
        time.sleep(0.1)
        
except KeyboardInterrupt:
    print("\nStopping...")
except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()