#!/usr/bin/env python3
# also the PyInstaller entry point for TermChat.exe
import sys

from termchat.gui import main

if __name__ == "__main__":
    sys.exit(main())
