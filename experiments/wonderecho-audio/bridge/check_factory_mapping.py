"""Validate bridge table against independently decoded factory command data."""
import json
import re
from pathlib import Path

source=Path(__file__).with_name('we_bridge_protocol.c').read_text()
rows=re.findall(r'\{(0x[0-9a-f]+),\s*(\d+),\s*(\d+),\s*"([^"]+)"\}',source)
data=json.loads(Path('tmp/voice-audit-20260919/bridge-image-comparison.json').read_text(encoding='utf-8'))
assert len(rows)==18
for group in data[0]['command_file']['groups']:
    commands={c['command_id']:c['phrase'] for c in group['commands']}
    for _,wire,command,phrase in rows:
        assert commands[int(command)]==phrase,(group['id'],wire,command,phrase)
assert len({(kind,wire) for kind,wire,_,_ in rows})==18
assert len({cid for _,_,cid,_ in rows})==18
print('PASS: 18 unique bridge mappings match both factory command groups')
