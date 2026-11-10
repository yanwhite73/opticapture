#!/bin/zsh
cd -- "${0:A:h}" || exit 1
python3 App/optimystery_capture.py keep "$@"
result=$?
if (( result )); then
  /usr/bin/osascript -e 'tell application "Terminal" to activate'
  read -r '?Press Return to close. '
fi
exit "$result"
