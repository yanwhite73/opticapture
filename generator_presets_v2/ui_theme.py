"""Native first-paint palettes. The same data is embedded in the Max runtime.

No runtime file dependency, host access, parameter creation or value writes.
"""
import json
from pathlib import Path

THEME = json.loads(Path(__file__).with_name('ui_theme.json').read_text())


def button_attributes(role='toggle', on=None):
    colors = THEME['colors']
    palette = THEME['buttons'][role]
    off, selected = colors[palette['off']], colors[palette['on']]
    off_text, selected_text = colors[palette['offText']], colors[palette['onText']]
    if on is not None:
        off = selected = selected if on else off
        off_text = selected_text = selected_text if on else off_text
    return dict(appearance=0, bgcolor=list(off), activebgcolor=list(off),
                bgoncolor=list(selected), activebgoncolor=list(selected),
                textoffcolor=list(off_text), textcolor=list(selected_text),
                activetextcolor=list(off_text), activetextoncolor=list(selected_text))
