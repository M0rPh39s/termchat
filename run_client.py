#!/usr/bin/env python3
"""Convenience launcher for the termchat client.

Equivalent to ``python -m termchat.client``.  For example::

    python run_client.py --host chat.example.com --port 9009
"""

import sys

from termchat.client import main

if __name__ == "__main__":
    sys.exit(main())
