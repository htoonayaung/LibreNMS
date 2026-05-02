#!/usr/bin/env python3
"""Simple restart supervisor for the local Flask app.

Usage:
  python gripcon.py
  python gripcon.py -- python app.py
  python gripcon.py --restart-delay 3 --max-restarts 0 -- python app.py

This script keeps the target command running, restarting it after any non-zero exit.
"""

import argparse
import os
import signal
import subprocess
import sys
import time

shutdown_requested = False


def handle_signal(signum, frame):
    global shutdown_requested
    shutdown_requested = True


def parse_args():
    parser = argparse.ArgumentParser(description='Keep a command running and restart it on failure.')
    parser.add_argument(
        '--restart-delay', '-d',
        type=float,
        default=2.0,
        help='Seconds to wait before restarting the process (default: 2.0)'
    )
    parser.add_argument(
        '--max-restarts', '-m',
        type=int,
        default=0,
        help='Maximum restart attempts before giving up (0 means unlimited)'
    )
    parser.add_argument(
        'command',
        nargs=argparse.REMAINDER,
        help='Command to run. If omitted, defaults to python app.py'
    )
    return parser.parse_args()


def main():
    global shutdown_requested
    args = parse_args()
    if args.command:
        command = args.command
    else:
        command = [sys.executable, 'app.py']

    if command and command[0] == '--':
        command = command[1:]

    command = [str(part) for part in command]

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    restart_count = 0
    while True:
        if shutdown_requested:
            print('Shutdown requested; stopping supervisor.')
            break

        restart_count += 1
        print(f'Starting command (attempt {restart_count}): {command}')
        process = subprocess.Popen(command, env=os.environ)

        try:
            exit_code = process.wait()
        except KeyboardInterrupt:
            shutdown_requested = True
            process.terminate()
            process.wait(timeout=5)
            break

        if shutdown_requested:
            print('Supervisor received shutdown signal.')
            break

        if exit_code == 0:
            print(f'Process exited cleanly with code {exit_code}; not restarting.')
            break

        print(f'Process exited with code {exit_code}.')
        if args.max_restarts > 0 and restart_count >= args.max_restarts:
            print(f'Maximum restart attempts reached ({args.max_restarts}); exiting.')
            break

        print(f'Waiting {args.restart_delay} seconds before restart...')
        for _ in range(int(args.restart_delay * 10)):
            if shutdown_requested:
                break
            time.sleep(0.1)
        if shutdown_requested:
            print('Shutdown requested during restart delay; exiting.')
            break

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
