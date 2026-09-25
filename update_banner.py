import re
from pathlib import Path

path = Path('llama_website/llama_game/index.html')
content = path.read_text()

transition_pattern = r'if \(\s*\(previousLevel === 2 && currentLevel\.number === 3\).*?\}, 500\);\n            \}'

new_transition = """
            if (previousLevel !== currentLevel.number && currentLevel.number > 1) {
              const transitionBanner = currentLevel.number === 4 ? castleBanner : hyperdriveBanner;
              if (currentLevel.number !== 4) {
                 transitionBanner.textContent = currentLevel.name.toUpperCase();
              }
              canvasWrap.classList.remove("hyperdrive-transition");
              transitionBanner.classList.remove("visible");
              void canvasWrap.offsetWidth;
              canvasWrap.classList.add("hyperdrive-transition");
              transitionBanner.classList.add("visible");
              window.setTimeout(() => {
                canvasWrap.classList.remove("hyperdrive-transition");
                transitionBanner.classList.remove("visible");
              }, 500);
            }
"""

content = re.sub(transition_pattern, new_transition.strip(), content, flags=re.DOTALL)
path.write_text(content)
