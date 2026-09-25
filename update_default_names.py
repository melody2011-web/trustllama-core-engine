import re
from pathlib import Path

path = Path('llama_website/llama_game/index.html')
content = path.read_text()

content = content.replace('The Highland Stroll', 'The Neon Reef')

path.write_text(content)
