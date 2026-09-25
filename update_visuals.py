import re
from pathlib import Path

path = Path('llama_website/llama_game/index.html')
content = path.read_text()

hazard_pattern = r'if \(reef\) \{\n            hazards\.sharks.*?\} else if \(farm\) \{\n            hazards\.tractors.*?\n          \}'
new_hazards = """
          if (reef) {
            hazards.sharks.filter(s => s.active).forEach((shark, i) => {
              const pulse = Math.sin(now / 150 + i) * 0.005;
              const r = shark.radius;
              context.save();
              context.translate(shark.x, shark.y);
              context.scale(Math.sin(now / 300), 1); // swimming motion
              
              context.fillStyle = "#002030";
              context.strokeStyle = "#00f0ff";
              context.lineWidth = 0.005;
              context.shadowColor = "#00f0ff";
              context.shadowBlur = 10 + pulse * 1000;
              
              context.beginPath();
              context.ellipse(0, 0, r * 1.5, r * 0.8, 0, 0, Math.PI * 2);
              context.fill(); context.stroke();
              
              context.beginPath();
              context.moveTo(-r * 0.2, -r * 0.5);
              context.lineTo(-r * 0.5, -r * 1.3);
              context.lineTo(r * 0.2, -r * 0.5);
              context.closePath();
              context.fill(); context.stroke();

              context.beginPath();
              context.moveTo(-r * 1.2, 0);
              context.lineTo(-r * 2.0, -r * 0.8);
              context.lineTo(-r * 1.8, 0);
              context.lineTo(-r * 2.0, r * 0.8);
              context.closePath();
              context.fill(); context.stroke();
              
              context.restore();
            });
          } else if (woods) {
            hazards.logs.filter(l => l.active).forEach((log, i) => {
              const r = log.radius;
              context.save();
              context.translate(log.x, log.y);
              context.rotate((now / 200) * (i % 2 === 0 ? 1 : -1)); // rolling motion
              
              context.fillStyle = "#1a0f08";
              context.strokeStyle = "#55ff55";
              context.lineWidth = 0.004;
              context.shadowColor = "#55ff55";
              context.shadowBlur = 10;
              
              context.beginPath();
              context.arc(0, 0, r, 0, Math.PI * 2);
              context.fill();
              context.stroke();
              
              context.strokeStyle = "#22bb22";
              context.lineWidth = 0.002;
              for(let ring = 1; ring < 4; ring++) {
                 context.beginPath();
                 context.arc(0, 0, r * (ring * 0.25), 0, Math.PI * 2);
                 context.stroke();
              }
              
              context.fillStyle = "#22bb22";
              for(let b=0; b<8; b++) {
                 context.beginPath();
                 context.arc(0, 0, r, b * Math.PI/4, b * Math.PI/4 + 0.15);
                 context.lineTo(0,0);
                 context.fill();
              }
              
              context.restore();
            });
          } else if (farm) {
            hazards.tractors.filter(t => t.active).forEach((tractor, i) => {
              const r = tractor.radius;
              context.save();
              context.translate(tractor.x, tractor.y);
              const bob = Math.sin(now / 50) * 0.005;
              context.translate(0, bob);
              
              context.shadowBlur = 15;
              context.shadowColor = "#ff9933";
              context.strokeStyle = "#ff9933";
              context.lineWidth = 0.004;
              
              context.fillStyle = "#330011";
              context.fillRect(-r*1.2, -r*0.6, r*2.4, r*1.2);
              context.strokeRect(-r*1.2, -r*0.6, r*2.4, r*1.2);
              
              context.fillStyle = "#222";
              context.fillRect(r*0.2, -r*1.2, r*0.6, r*0.6);
              context.strokeRect(r*0.2, -r*1.2, r*0.6, r*0.6);
              
              context.fillStyle = "#000";
              context.beginPath();
              context.arc(-r*0.6, r*0.6, r*0.5, 0, Math.PI * 2);
              context.fill(); context.stroke();
              
              context.beginPath();
              context.arc(r*0.6, r*0.6, r*0.8, 0, Math.PI * 2);
              context.fill(); context.stroke();
              
              context.fillStyle = "#ff9933";
              context.beginPath();
              context.arc(-r*0.6, r*0.6, r*0.2, 0, Math.PI * 2);
              context.fill();
              
              context.beginPath();
              context.arc(r*0.6, r*0.6, r*0.3, 0, Math.PI * 2);
              context.fill();
              
              context.restore();
            });
          }
"""

content = re.sub(hazard_pattern, new_hazards.strip(), content, flags=re.DOTALL)
path.write_text(content)
