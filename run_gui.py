#!/usr/bin/env python3
"""Launcher for the termchat desktop GUI client.

Usage::

    python run_gui.py

This is also the PyInstaller entry point for building TermChat.exe.
"""

import sys

from termchat.gui import main

if __name__ == "__main__":
    sys.exit(main())
