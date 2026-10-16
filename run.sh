#!/bin/bash
# Starts Breathing in the menu bar. Safe to close this Terminal window afterwards.
cd "$(dirname "$0")"
pkill -f "[Pp]ython breathing.py" && sleep 1  # restart instead of running two copies
nohup .venv/bin/python breathing.py > breathing.log 2>&1 &
sleep 3
if kill -0 $! 2>/dev/null; then
  echo "Breathing is running — look for 🤐 in your menu bar."
else
  echo "Breathing failed to start. Details:"
  tail -20 breathing.log
fi
