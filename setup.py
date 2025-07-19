from setuptools import setup

APP = ['MultinomeSeqV2.5.py']
APP_NAME = "Multinome Sequencer"
ICON_FILE = 'Multinome.icns'

OPTIONS = {
    'argv_emulation': False,
    'packages': ['rtmidi', 'monome'],
    'includes': ['monome.serialosc', 'monome.grid', 'monome.events', 'monome.led'],
    'iconfile': ICON_FILE,
    'plist': {
        'CFBundleName': APP_NAME,
        'CFBundleDisplayName': APP_NAME,
        'CFBundleGetInfoString': "A polyrhythmic sequencer for Monome grids",
        'CFBundleIdentifier': "com.julio.multinomesequencer", # Change this to be unique
        'CFBundleVersion': "2.4.0",
        'CFBundleShortVersionString': "2.4",
        'NSHumanReadableCopyright': "Copyright © 2024, Julio Figueroa, All Rights Reserved"
    }
}

setup(
    app=APP,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
