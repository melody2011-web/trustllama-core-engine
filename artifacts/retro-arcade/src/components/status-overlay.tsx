import { useArcadeEngine } from '@/hooks/use-arcade-engine';

export function StatusOverlay() {
  const { connection, player, actions } = useArcadeEngine();

  // Show "GAME OVER" if player is dead
  if (player?.game_over) {
    return (
      <div className="absolute inset-0 bg-black/80 z-40 flex items-center justify-center p-4 backdrop-blur-md" data-testid="overlay-game-over">
        <div className="text-center">
          <h2 className="font-pixel text-6xl text-destructive text-glow-primary mb-4 animate-pulse">
            GAME OVER
          </h2>
          <p className="font-mono text-xl text-white mb-8">
            FINAL SCORE: <span className="text-accent">{player.score.toString().padStart(6, '0')}</span>
          </p>
           <button
             type="button"
             onClick={actions.reset}
             className="font-pixel bg-primary px-5 py-3 text-black"
             data-testid="button-reset-game"
           >
             NEW TICKET
           </button>
        </div>
      </div>
    );
  }

  // If connected but ticket is missing or invalid, show a warning, but honestly.
  if (connection.status === 'connected' && connection.ticketStatus === 'error') {
    return (
      <div className="absolute top-1/4 left-1/2 -translate-x-1/2 bg-destructive/90 text-white font-mono p-4 border-4 border-white z-50 shadow-2xl text-center max-w-sm" data-testid="overlay-ticket-error">
        <h3 className="font-bold text-xl mb-2">TICKET REJECTED</h3>
        <p className="text-sm">Cannot verify Tlama payment. Cabinet locked.</p>
      </div>
    );
  }
  
  if (connection.status === 'connected' && connection.ticketStatus === 'pending') {
    return (
      <div className="absolute top-1/4 left-1/2 -translate-x-1/2 bg-accent/90 text-black font-mono p-4 border-4 border-white z-50 shadow-2xl text-center max-w-sm" data-testid="overlay-ticket-pending">
        <h3 className="font-bold text-xl mb-2 animate-pulse">VERIFYING PAYMENT</h3>
        <p className="text-sm">Awaiting on-chain confirmation...</p>
      </div>
    );
  }

  return null;
}
