import re
from pathlib import Path

path = Path('llama_website/llama_game/index.html')
content = path.read_text()

content = content.replace('message.event === "emp_stun"', 'message.event === "hazard_stun"')
content = content.replace('message.event === "ghost_stun" || message.event === "emp_stun"', 'message.event === "ghost_stun" || message.event === "hazard_stun"')

path.write_text(content)
