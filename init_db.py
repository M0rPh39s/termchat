#!/usr/bin/env python3
"""Initialise (or inspect) the termchat SQLite database.

Creating the schema is normally automatic — the server calls it on start
— but this script lets you set things up ahead of time, seed the default
channels, and optionally create an initial user.

Usage::

    python init_db.py                      # create schema + default channels
    python init_db.py --db chat.db         # custom path
    python init_db.py --add-user alice     # also create a user (prompts pw)
    python init_db.py --show               # print a summary of the database
"""

import argparse
import getpass
import sys

from termchat.database import Database


def main(argv=None):
    parser = argparse.ArgumentParser(description="termchat database tool")
    parser.add_argument("--db", default="termchat.db",
                        help="database path (default: termchat.db)")
    parser.add_argument("--add-user", metavar="USERNAME",
                        help="create a user (you'll be prompted for a password)")
    parser.add_argument("--show", action="store_true",
                        help="print a summary of the database contents")
    args = parser.parse_args(argv)

    db = Database(args.db)
    print(f"database ready at {args.db}")

    channels = db.list_channels()
    print(f"channels: {', '.join('#' + c['name'] for c in channels)}")

    if args.add_user:
        password = getpass.getpass(f"password for {args.add_user}: ")
        confirm = getpass.getpass("confirm password: ")
        if password != confirm:
            print("passwords do not match", file=sys.stderr)
            return 1
        user_id, error = db.create_user(args.add_user, password)
        if error:
            print(f"could not create user: {error}", file=sys.stderr)
            return 1
        print(f"created user {args.add_user!r} (id={user_id})")

    if args.show:
        print("\n-- summary --")
        for c in db.list_channels():
            history = db.get_history(c["id"], limit=1000)
            print(f"  #{c['name']:<16} messages={len(history):<5} "
                  f"members={c['member_count']}")

    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
