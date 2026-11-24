#!/bin/bash
cd "$(dirname "$0")" || exit 1
python3 launcher.py "$@"
task_result=$?
if [ "$task_result" -ne 0 ]; then
  read -r -p "Press Enter to close."
fi
exit "$task_result"
