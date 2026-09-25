import re
from pathlib import Path

path = Path('llama_website/llama_game/index.html')
content = path.read_text()

# Replace state variables
content = content.replace('let hazard = null;', 'let hazards = { sharks: [], logs: [], tractors: [] };')
content = content.replace('if (state.hazard) hazard = state.hazard;', 'if (state.sharks) hazards.sharks = state.sharks;\n          if (state.logs) hazards.logs = state.logs;\n          if (state.tractors) hazards.tractors = state.tractors;')

# Replace currentLevel variables
content = content.replace('const hyperDrive = currentLevel.number === 3;', 'const reef = currentLevel.number === 1;\n          const woods = currentLevel.number === 2;\n          const farm = currentLevel.number === 3;')

# Rewrite background drawing block
bg_pattern = r'const background = context\.createLinearGradient\(0, 0, 1, 1\);.*?context\.shadowBlur = 0;'
new_bg = """
          const background = context.createLinearGradient(0, 0, 1, 1);
          if (reef) {
            background.addColorStop(0, "#011225"); background.addColorStop(1, "#0a2e3f");
          } else if (woods) {
            background.addColorStop(0, "#05180f"); background.addColorStop(1, "#12301c");
          } else if (farm) {
            background.addColorStop(0, "#1c0a1a"); background.addColorStop(1, "#361622");
          } else {
            background.addColorStop(0, "#071119"); background.addColorStop(1, "#230c31");
          }
          context.fillStyle = background;
          context.fillRect(0, 0, 1, 1);

          context.strokeStyle = reef ? "rgba(0, 236, 255, 0.4)" : woods ? "rgba(100, 255, 100, 0.4)" : farm ? "rgba(255, 150, 50, 0.4)" : "rgba(255, 154, 98, 0.5)";
          context.shadowBlur = reef || woods || farm ? 0.015 : 0;
          context.shadowColor = reef ? "#00ecff" : woods ? "#55ff55" : farm ? "#ff9933" : "#000";
          context.lineWidth = 0.004;
          context.strokeRect(
            currentLevel.board_min,
            currentLevel.board_min,
            currentLevel.board_max - currentLevel.board_min,
            currentLevel.board_max - currentLevel.board_min
          );
          const gridPulse = (reef || woods || farm) ? 1 + Math.sin(now / 240) * 0.055 : 1;
          const gridStep = 0.08 * gridPulse;
          context.lineWidth = (reef || woods || farm) ? 0.0022 : 0.001;
          for (let line = gridStep; line < 1; line += gridStep) {
            context.strokeStyle = reef ? "rgba(0, 150, 255, 0.15)" : woods ? "rgba(50, 200, 50, 0.15)" : farm ? "rgba(200, 50, 150, 0.15)" : "rgba(179, 193, 255, 0.08)";
            context.shadowColor = context.strokeStyle;
            context.beginPath(); context.moveTo(line, 0); context.lineTo(line, 1); context.stroke();
            context.beginPath(); context.moveTo(0, line); context.lineTo(1, line); context.stroke();
          }
          context.shadowBlur = 0;
"""
content = re.sub(bg_pattern, new_bg.strip(), content, flags=re.DOTALL)

# Remove grid_glitch rendering and add sharks, logs, tractors
hazard_pattern = r'if \(hyperDrive && hazard && hazard\.active\) \{.*?context\.shadowBlur = 0;\n          \}'

new_hazards = """
          if (reef) {
            hazards.sharks.filter(s => s.active).forEach((shark, i) => {
              const pulse = Math.sin(now / 150 + i) * 0.005;
              context.fillStyle = "#00f0ff";
              context.shadowColor = "#00f0ff";
              context.shadowBlur = 15;
              context.beginPath();
              context.arc(shark.x, shark.y, shark.radius + pulse, 0, Math.PI * 2);
              context.fill();
              context.shadowBlur = 0;
            });
          } else if (woods) {
            hazards.logs.filter(l => l.active).forEach((log, i) => {
              context.fillStyle = "#66ff66";
              context.shadowColor = "#66ff66";
              context.shadowBlur = 15;
              context.beginPath();
              context.arc(log.x, log.y, log.radius, 0, Math.PI * 2);
              context.fill();
              context.shadowBlur = 0;
            });
          } else if (farm) {
            hazards.tractors.filter(t => t.active).forEach((tractor, i) => {
              context.fillStyle = "#ff3399";
              context.shadowColor = "#ff3399";
              context.shadowBlur = 15;
              context.beginPath();
              context.arc(tractor.x, tractor.y, tractor.radius, 0, Math.PI * 2);
              context.fill();
              context.shadowBlur = 0;
            });
          }
"""
content = re.sub(hazard_pattern, new_hazards.strip(), content, flags=re.DOTALL)

# Handle hyperDrive references that were missed
content = content.replace('if (hyperDrive || castle)', 'if (farm || castle)')

path.write_text(content)
