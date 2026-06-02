#!/usr/bin/env python3
"""Alex — an agent with web tools, interactive TUI mode.

Usage:
    python main.py

Keyboard shortcuts:
    Ctrl+T    Toggle thinking expanded/collapsed
    Ctrl+K    Toggle skill blocks
    Ctrl+G    Rate last response good
    Ctrl+B    Rate last response bad
    Ctrl+C    Quit
"""

from alex.entry import main

if __name__ == "__main__":
    main()
