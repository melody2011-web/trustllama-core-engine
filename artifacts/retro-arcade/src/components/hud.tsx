import { useArcadeEngine } from '@/hooks/use-arcade-engine';

export function HUD() {
  const { player, state, actions } = useArcadeEngine();

  if (!player) return null;

  return (
    <div className="absolute top-0 left-0 w-full p-2.5 sm:p-4 pointer-events-none z-10" data-testid="overlay-hud">
      <div className="flex justify-between items-start gap-2">
        {/* Top Left: Score & Player */}
        <div className="min-w-0 flex flex-col gap-1 bg-black/80 px-3 py-2.5 sm:p-3 border-2 border-primary">
          <div className="font-pixel text-primary text-base sm:text-xl uppercase tracking-[0.12em] sm:tracking-[0.18em] leading-tight whitespace-nowrap text-glow-primary" data-testid="text-player-name">
            {player.name}
          </div>
          <div className="font-mono text-white text-sm sm:text-lg flex items-center gap-1.5 sm:gap-2 whitespace-nowrap">
            <span className="text-muted-foreground">SCORE</span>
            <span className="text-accent text-glow-accent font-bold" data-testid="text-score">
              {player.score.toString().padStart(6, '0')}
            </span>
          </div>
        </div>

        {/* Top Right: Lives / Heart Matrix */}
        <div className="shrink-0 flex flex-col gap-1 bg-black/80 px-3 py-2.5 sm:p-3 border-2 border-secondary items-end">
          <div className="font-pixel text-secondary text-base sm:text-xl uppercase tracking-[0.12em] sm:tracking-widest text-glow-secondary">
            MATRIX
          </div>
          <div className="flex items-center gap-1 mt-1" data-testid="list-lives">
            {Array.from({ length: player.max_lives ?? state.starting_lives }).map((_, i) => {
              const active = i < player.lives;
              return (
                <div 
                  key={i} 
                  className={`w-4 h-4 border ${active ? 'bg-destructive border-destructive shadow-[0_0_8px_red]' : 'bg-transparent border-muted'}`}
                  style={{ clipPath: 'polygon(50% 100%, 0 40%, 0 0, 40% 0, 50% 20%, 60% 0, 100% 0, 100% 40%)' }}
                />
              );
            })}
          </div>
          <button 
            className="pointer-events-auto mt-2 text-xs bg-secondary text-secondary-foreground px-2 py-1 uppercase font-bold hover:bg-white transition-colors"
            onClick={() => actions.purchaseHeartUpgrade()}
            disabled={
              !player.extra_life_unlocked ||
              (player.extra_lives_purchased ?? 0) >=
                (state.extra_life_catalog[0]?.max_per_match ?? 2)
            }
            data-testid="button-buy-heart"
          >
            {!player.extra_life_unlocked
              ? 'UNLOCKS AT LEVEL 6'
              : (player.extra_lives_purchased ?? 0) >=
                  (state.extra_life_catalog[0]?.max_per_match ?? 2)
                ? 'MAX UPGRADES'
                : `BUY 1UP (${state.extra_life_catalog[0]?.amount_tlama ?? 150} TLAMA)`}
          </button>
        </div>
      </div>
    </div>
  );
}

export function StageSelector() {
  const { player, state } = useArcadeEngine();

  if (!player) return null;

  return (
    <div className="w-full flex justify-center px-1 pt-2 shrink-0" data-testid="stage-selector">
      <div className="max-w-full bg-black border-2 border-accent px-1.5 sm:px-2 py-1.5 flex items-center gap-1 sm:gap-2">
        <span className="font-pixel text-accent text-[10px] sm:text-sm uppercase shrink-0">Stage</span>
        <div className="flex gap-0.5 sm:gap-1 shrink-0">
          {state.levels.map((level) => {
            const isCurrent = level.number === player.current_level;
            const isPast = level.number < player.current_level;
            return (
              <div
                key={level.number}
                className={`w-5 h-5 sm:w-6 sm:h-6 flex items-center justify-center font-mono text-[10px] sm:text-xs font-bold border ${
                  isCurrent
                    ? 'bg-accent text-black border-accent animate-pulse'
                    : isPast
                      ? 'bg-success/20 text-success border-success'
                      : 'bg-transparent text-muted-foreground border-muted'
                }`}
                data-testid={`status-stage-${level.number}`}
              >
                {level.number}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
