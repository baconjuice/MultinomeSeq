from setuptools import setup

APP = ['MultinomeSeqV2.4.py']
APP_NAME = "Multinome Sequencer"
ICON_FILE = 'Multinome.icns'

# The icon file needs to be included in the app bundle
DATA_FILES = [ICON_FILE]

OPTIONS = {
    'argv_emulation': True,
    'packages': ['rtmidi', 'monome'], # tkinter is part of stdlib
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
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
