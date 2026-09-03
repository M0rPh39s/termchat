#!/usr/bin/env python3
"""Convenience launcher for the termchat web UI.

Equivalent to ``python -m termchat.web``.  All arguments are passed
straight through, e.g.::

    python run_web.py --port 8080 --ngrok
"""

from termchat.web import main

if __name__ == "__main__":
    raise SystemExit(main())
