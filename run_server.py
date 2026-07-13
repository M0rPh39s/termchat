#!/usr/bin/env python3
"""Convenience launcher for the termchat server.

Equivalent to ``python -m termchat.server``.  All arguments are passed
straight through, e.g.::

    python run_server.py --host 0.0.0.0 --port 9009 --db termchat.db
"""

from termchat.server import main

if __name__ == "__main__":
    main()
