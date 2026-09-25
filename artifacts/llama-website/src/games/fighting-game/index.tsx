import { useState, useEffect, useRef, useCallback } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Card } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Swords, Ghost, RefreshCcw, WifiOff, ChevronUp, ChevronDown, ChevronLeft, ChevronRight, Fingerprint } from 'lucide-react';
import { useFighter } from './useFighter';
import { Renderer } from './Renderer';

function useKeyboardAndTouch(sendAction: (action: string) => void, isActive: boolean) {
  const keys = useRef<Set<string>>(new Set());
  const lastAction = useRef<string>('idle');
  const heldAction = useRef<string>('idle');
  const actionTimeout = useRef<ReturnType<typeof setTimeout> | null>(null);

  const processKeys = useCallback(() => {
    if (!isActive) return;
    let action = 'idle';
    if (keys.current.has('light_punch')) action = 'light_punch';
    else if (keys.current.has('heavy_kick')) action = 'heavy_kick';
    else if (keys.current.has('block')) action = 'block';
    else if (keys.current.has('jump')) action = 'jump';
    else if (keys.current.has('crouch')) action = 'crouch';
    else if (keys.current.has('move_left') && keys.current.has('move_right')) action = 'idle';
    else if (keys.current.has('move_left')) action = 'move_left';
    else if (keys.current.has('move_right')) action = 'move_right';
    heldAction.current = action;

    if (action !== lastAction.current) {
      lastAction.current = action;
      sendAction(action);

       if (['light_punch', 'heavy_kick'].includes(action)) {
          if (actionTimeout.current) clearTimeout(actionTimeout.current);
          actionTimeout.current = setTimeout(() => {
             keys.current.delete(action);
             processKeys();
          }, 200);
      }
    }
  }, [isActive, sendAction]);

  useEffect(() => {
    if (!isActive) return;
    const interval = window.setInterval(() => {
      if (
        heldAction.current === 'move_left'
        || heldAction.current === 'move_right'
      ) {
        sendAction(heldAction.current);
      }
    }, 70);
    return () => window.clearInterval(interval);
  }, [isActive, sendAction]);

  useEffect(() => {
    const keyMap: Record<string, string> = {
      'a': 'move_left',
      'd': 'move_right',
      'w': 'jump',
      's': 'crouch',
      'j': 'light_punch',
      'k': 'heavy_kick',
      'l': 'block',
      'arrowleft': 'move_left',
      'arrowright': 'move_right',
      'arrowup': 'jump',
      'arrowdown': 'crouch'
    };

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement) return;
      const k = e.key.toLowerCase();
      if (keyMap[k] && !e.repeat) {
        keys.current.add(keyMap[k]);
        processKeys();
      }
    };

    const onKeyUp = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement) return;
      const k = e.key.toLowerCase();
      if (keyMap[k]) {
        keys.current.delete(keyMap[k]);
        processKeys();
      }
    };

    window.addEventListener('keydown', onKeyDown);
    window.addEventListener('keyup', onKeyUp);
    return () => {
      window.removeEventListener('keydown', onKeyDown);
      window.removeEventListener('keyup', onKeyUp);
    };
  }, [processKeys]);

  const onTouchStart = useCallback((action: string) => () => {
    keys.current.add(action);
    processKeys();
  }, [processKeys]);

  const onTouchEnd = useCallback((action: string) => () => {
    keys.current.delete(action);
    processKeys();
  }, [processKeys]);

  useEffect(() => {
      const handleMouseUp = () => {
          if (keys.current.size > 0) {
              keys.current.clear();
              processKeys();
          }
      };
      window.addEventListener('mouseup', handleMouseUp);
      window.addEventListener('blur', handleMouseUp);
      return () => {
          window.removeEventListener('mouseup', handleMouseUp);
          window.removeEventListener('blur', handleMouseUp);
          if (actionTimeout.current) clearTimeout(actionTimeout.current);
          keys.current.clear();
          heldAction.current = 'idle';
      };
  }, [processKeys]);

  return { onTouchStart, onTouchEnd };
}

export function FightingGame() {
  const { gameState, error, connecting, createMatch, sendAction, selectCharacter, credentials } = useFighter();
  const [name, setName] = useState('');
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const rendererRef = useRef<Renderer | null>(null);
  const isVisualTest = import.meta.env.MODE === 'visual-test';

  useEffect(() => {
    const previousTitle = document.title;
    document.title = 'Matrix Duel: Micro-Strike | Premium Silhouette Mode';
    return () => {
      document.title = previousTitle;
    };
  }, []);

  useEffect(() => {
    if (canvasRef.current && !rendererRef.current && gameState?.status !== 'character_select' && gameState) {
      rendererRef.current = new Renderer(canvasRef.current);
      if (!isVisualTest) rendererRef.current.start();
    }
    return () => {
      if (rendererRef.current) {
        rendererRef.current.stop();
        rendererRef.current = null;
      }
    };
  }, [gameState?.match_id, isVisualTest]);

  useEffect(() => {
    if (rendererRef.current) {
      rendererRef.current.state = gameState;
      rendererRef.current.localPlayerId = credentials?.playerId || null;
      if (isVisualTest) rendererRef.current.renderStatic();
    }
  }, [gameState, credentials, isVisualTest]);

  useEffect(() => {
    const onResize = () => {
      if (canvasRef.current) {
        const parent = canvasRef.current.parentElement;
        if (parent) {
           canvasRef.current.width = parent.clientWidth;
           canvasRef.current.height = parent.clientHeight;
           if (isVisualTest && rendererRef.current) rendererRef.current.renderStatic();
        }
      }
    };
    window.addEventListener('resize', onResize);
    onResize();
    return () => window.removeEventListener('resize', onResize);
  }, [gameState?.status, isVisualTest]);

  const isActive = gameState?.status === 'active';
  const { onTouchStart, onTouchEnd } = useKeyboardAndTouch(sendAction, isActive);

  if (!gameState) {
    return (
      <div className="min-h-[100dvh] flex flex-col items-center justify-center p-4 bg-background game-platform-theme relative overflow-hidden">
        <div className="absolute inset-0 bg-grid opacity-20 pointer-events-none" />
        <Card className="w-full max-w-md p-8 space-y-8 border-2 border-primary/40 bg-black/60 backdrop-blur-xl shadow-[0_0_40px_rgba(0,255,255,0.15)] relative z-10">
          <div className="text-center space-y-4">
            <h1 className="text-4xl font-arcade text-primary drop-shadow-[0_0_15px_rgba(0,255,255,0.6)] flex flex-col items-center gap-4">
              <div className="p-4 rounded-full bg-primary/10 border border-primary/30 shadow-[0_0_20px_rgba(0,255,255,0.2)]">
                <Swords className="w-12 h-12 text-primary" />
              </div>
              <span className="leading-tight">MATRIX DUEL:</span>
              <span className="-mt-3 text-3xl leading-tight">MICRO-STRIKE</span>
            </h1>
            <p className="text-primary/70 text-sm font-mono tracking-[0.3em] uppercase">Premium Silhouette Mode</p>
          </div>

          {error && (
            <div className="bg-destructive/10 text-destructive p-3 rounded-md text-sm border border-destructive/50 text-center font-mono shadow-[0_0_10px_rgba(239,68,68,0.2)]">
              {error}
            </div>
          )}

          <form className="space-y-6" onSubmit={(e) => { e.preventDefault(); createMatch(name); }}>
            <div className="space-y-2">
              <label className="text-xs uppercase text-primary/70 font-mono tracking-wider flex items-center gap-2">
                <Fingerprint className="w-4 h-4" /> FIGHTER DESIGNATION
              </label>
              <Input
                value={name}
                onChange={e => setName(e.target.value)}
                maxLength={24}
                className="font-mono bg-black/80 border-primary/40 text-lg py-6 focus-visible:ring-primary focus-visible:border-primary shadow-inner"
                placeholder="ENTER NAME"
                data-testid="input-displayname"
              />
            </div>

            <Button
              type="submit"
              className="w-full h-14 text-lg font-arcade tracking-widest border border-primary hover:bg-primary hover:text-black bg-primary/20 text-primary transition-all duration-300 shadow-[0_0_20px_rgba(0,255,255,0.2)] hover:shadow-[0_0_30px_rgba(0,255,255,0.6)]"
              disabled={name.length < 2 || connecting}
              data-testid="btn-create-match"
            >
              {connecting ? 'INITIALIZING...' : 'INITIALIZE LINK'}
            </Button>
          </form>
        </Card>
      </div>
    );
  }

  if (gameState.status === 'character_select') {
    return (
      <div className="min-h-[100dvh] flex flex-col items-center justify-center p-4 bg-[#050510] game-platform-theme relative overflow-hidden">
         <div className="absolute inset-0 bg-grid opacity-20 pointer-events-none" />
         <div className="absolute top-0 left-0 w-full h-64 bg-gradient-to-b from-primary/10 to-transparent pointer-events-none" />

         <div className="text-center mb-16 relative z-10">
            <p className="mb-5 font-mono text-xs uppercase tracking-[0.35em] text-primary/60">
              Matrix Duel: Micro-Strike · Premium Silhouette Mode
            </p>
            <h2 className="text-4xl md:text-6xl font-arcade text-primary drop-shadow-[0_0_20px_rgba(0,255,255,0.6)] mb-4">SELECT CHASSIS</h2>
            <p className="text-primary/70 font-mono tracking-[0.3em] animate-pulse">AWAITING INPUT...</p>
         </div>

         <div className="flex flex-col md:flex-row justify-center gap-6 md:gap-8 w-full max-w-5xl relative z-10 px-4">
            {[
              { id: 'andean-guardian', name: 'ANDEAN GUARDIAN', color: 'text-amber-500', bg: 'hover:bg-amber-500/10', border: 'border-amber-500/30 hover:border-amber-500', shadow: 'hover:shadow-[0_0_40px_rgba(245,158,11,0.3)]', glow: 'drop-shadow-[0_0_15px_rgba(245,158,11,0.8)]' },
              { id: 'neon-puma', name: 'NEON PUMA', color: 'text-pink-500', bg: 'hover:bg-pink-500/10', border: 'border-pink-500/30 hover:border-pink-500', shadow: 'hover:shadow-[0_0_40px_rgba(236,72,153,0.3)]', glow: 'drop-shadow-[0_0_15px_rgba(236,72,153,0.8)]' },
              { id: 'quantum-llama', name: 'QUANTUM LLAMA', color: 'text-purple-500', bg: 'hover:bg-purple-500/10', border: 'border-purple-500/30 hover:border-purple-500', shadow: 'hover:shadow-[0_0_40px_rgba(168,85,247,0.3)]', glow: 'drop-shadow-[0_0_15px_rgba(168,85,247,0.8)]' }
            ].map(char => (
              <Card
                key={char.id}
                className={`flex-1 p-8 cursor-pointer transition-all duration-300 border-2 ${char.border} ${char.bg} ${char.shadow} bg-black/60 backdrop-blur-md text-center group`}
                onClick={() => selectCharacter(char.id)}
                data-testid={`btn-select-${char.id}`}
              >
                <div className={`h-40 mb-8 flex items-center justify-center transition-transform duration-500 group-hover:scale-110 group-active:scale-95`}>
                  <Ghost className={`w-32 h-32 ${char.color} ${char.glow} transition-all duration-300`} strokeWidth={1.5} />
                </div>
                <h3 className={`font-arcade text-2xl ${char.color} tracking-[0.2em] group-hover:${char.glow}`}>{char.name}</h3>
              </Card>
            ))}
         </div>
      </div>
    );
  }

  const players = Object.values(gameState.players);
  const p1 = players.find(p => p.player_id === credentials?.playerId) || players[0];
  const p2 = players.find(p => p.player_id !== p1?.player_id) || players[1];

  return (
    <div className="min-h-[100dvh] w-full bg-[#020208] game-platform-theme flex flex-col relative overflow-hidden select-none touch-none">
      {/* HUD */}
      <div className="relative z-10 w-full p-3 sm:p-4 md:p-8 flex justify-between items-start gap-2 sm:gap-4 pointer-events-none">
        {/* P1 Stats */}
        <div className="flex-1 max-w-[42%] space-y-3 relative">
          <div className="flex justify-between items-end px-2 border-b-[3px] border-cyan-400 pb-1 shadow-[0_4px_10px_rgba(34,211,238,0.2)]">
            <span data-testid="name-p1" className="min-w-0 font-arcade text-[11px] sm:text-xl md:text-4xl text-cyan-400 drop-shadow-[0_0_10px_rgba(34,211,238,0.8)] truncate tracking-normal sm:tracking-wider">{p1?.display_name || 'P1'}</span>
            <span className="font-mono text-xs md:text-sm text-cyan-400/60 tracking-widest hidden md:inline">{p1?.character_id?.replace('-', ' ').toUpperCase()}</span>
          </div>
          <div className="h-6 md:h-10 w-full bg-black/90 border-[3px] border-cyan-400 relative overflow-hidden shadow-[0_0_20px_rgba(34,211,238,0.3)] skew-x-[-15deg] flex justify-end p-1">
            <div
              className="h-full bg-cyan-400 transition-all duration-300 ease-out shadow-[0_0_15px_rgba(34,211,238,1)]"
              style={{ width: `${p1?.health ?? 100}%` }}
              data-testid="health-p1"
            />
          </div>
          {p1?.combo?.active && p1.combo.hits > 1 && (
            <div className="animate-fade-in flex items-baseline gap-3 pt-2">
              <span className="font-arcade text-4xl text-amber-400 drop-shadow-[0_0_15px_rgba(251,191,36,0.8)] italic">{p1.combo.hits} HITS</span>
              <span className="font-mono text-sm text-amber-400/80 tracking-widest">{p1.combo.total_damage} DMG</span>
            </div>
          )}
        </div>

        {/* Timer */}
        <div className="flex flex-col items-center justify-start shrink-0 relative z-20 mx-1 sm:mx-2">
          <span className="mb-2 hidden whitespace-nowrap font-mono text-[9px] uppercase tracking-[0.24em] text-white/50 lg:block">
            Matrix Duel: Micro-Strike
          </span>
          <div className="bg-black/90 border-2 border-white/20 px-3 py-2 sm:px-6 sm:py-4 rounded-xl skew-x-[-10deg] shadow-[0_0_30px_rgba(255,255,255,0.1)] backdrop-blur-md">
            <span
              className="font-arcade text-3xl sm:text-5xl md:text-7xl text-white drop-shadow-[0_0_20px_rgba(255,255,255,1)] leading-none block skew-x-[10deg]"
              data-testid="text-timer"
            >
              {gameState.round_seconds_remaining}
            </span>
          </div>
          {connecting && (
            <Badge variant="outline" className="mt-6 text-destructive border-destructive animate-pulse gap-2 bg-destructive/10 px-4 py-1 text-xs tracking-widest backdrop-blur-md">
              <WifiOff className="w-4 h-4" /> RECONNECTING
            </Badge>
          )}
          <span
            className="mt-4 hidden rounded-full border border-white/10 bg-black/60 px-4 py-1.5 font-mono text-[10px] tracking-widest text-white/60 sm:block backdrop-blur-sm"
            data-testid="text-entry-quote"
          >
            ENTRY ${gameState.entry_validation.target_usd} · {gameState.entry_validation.amount_tlama} TLAMA
          </span>
        </div>

        {/* P2 Stats */}
        <div className="flex-1 max-w-[42%] space-y-3 relative">
          <div className="flex justify-between items-end px-2 border-b-[3px] border-fuchsia-500 pb-1 flex-row-reverse shadow-[0_4px_10px_rgba(217,70,239,0.2)]">
            <span data-testid="name-p2" className="min-w-0 font-arcade text-[11px] sm:text-xl md:text-4xl text-fuchsia-500 drop-shadow-[0_0_10px_rgba(217,70,239,0.8)] truncate tracking-normal sm:tracking-wider">{p2?.display_name || 'AI'}</span>
            <span className="font-mono text-xs md:text-sm text-fuchsia-500/60 tracking-widest hidden md:inline">{p2?.character_id?.replace('-', ' ').toUpperCase()}</span>
          </div>
          <div className="h-6 md:h-10 w-full bg-black/90 border-[3px] border-fuchsia-500 relative overflow-hidden shadow-[0_0_20px_rgba(217,70,239,0.3)] skew-x-[15deg] flex justify-start p-1">
            <div
              className="h-full bg-fuchsia-500 transition-all duration-300 ease-out shadow-[0_0_15px_rgba(217,70,239,1)]"
              style={{ width: `${p2?.health ?? 100}%` }}
              data-testid="health-p2"
            />
          </div>
          {p2?.combo?.active && p2.combo.hits > 1 && (
            <div className="animate-fade-in flex flex-row-reverse items-baseline gap-3 pt-2">
              <span className="font-arcade text-4xl text-amber-400 drop-shadow-[0_0_15px_rgba(251,191,36,0.8)] italic">{p2.combo.hits} HITS</span>
              <span className="font-mono text-sm text-amber-400/80 tracking-widest">{p2.combo.total_damage} DMG</span>
            </div>
          )}
        </div>
      </div>

      {/* Game Canvas */}
      <div className="flex-1 relative w-full flex items-center justify-center min-h-[50vh]">
        <canvas
          ref={canvasRef}
          className="w-full h-full object-contain pointer-events-none"
          data-testid="game-canvas"
        />
      </div>

      {/* Round Complete Overlay */}
      {gameState.status === 'round_complete' && (
        <div className="absolute inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-md animate-fade-in pointer-events-auto">
          <div className="text-center space-y-10 p-16 border-2 border-primary/40 bg-black/60 rounded-2xl shadow-[0_0_60px_rgba(0,255,255,0.2)]">
            <h2 className="text-7xl md:text-8xl font-arcade text-white drop-shadow-[0_0_30px_rgba(255,255,255,0.8)] tracking-[0.1em]">
              {gameState.winner_slot === p1?.slot ? 'VICTORY' : 'DEFEAT'}
            </h2>
            <div className="font-mono text-2xl text-primary/80 uppercase tracking-[0.3em]">
              {gameState.winner_slot === p1?.slot
                ? 'TARGET ELIMINATED'
                : 'CHASSIS DESTROYED'}
            </div>
            <Button
              size="lg"
              className="mt-8 font-arcade text-2xl h-16 px-12 border-2 border-primary hover:bg-primary hover:text-black bg-primary/20 text-primary transition-all duration-300 shadow-[0_0_20px_rgba(0,255,255,0.3)] hover:shadow-[0_0_40px_rgba(0,255,255,0.6)] hover:scale-105"
              onClick={() => window.location.reload()}
              data-testid="btn-rematch"
            >
              <RefreshCcw className="mr-4 w-6 h-6" /> INITIALIZE REMATCH
            </Button>
          </div>
        </div>
      )}

      {/* Controls Overlay */}
      <div className="absolute bottom-3 left-2 right-2 z-40 flex justify-between items-end pointer-events-none sm:bottom-6 sm:left-6 sm:right-6">
        {/* Desktop Keyboard Hints */}
        <div className="hidden md:grid grid-cols-2 gap-x-12 gap-y-4 p-8 bg-[#050505]/90 border-[3px] border-cyan-400/80 rounded-none font-mono text-sm text-cyan-400/80 backdrop-blur-md shadow-[10px_10px_0px_#000]">
           <div><b className="text-cyan-400 inline-block w-8 drop-shadow-[0_0_5px_currentColor]">A/D</b> MOVE</div>
           <div><b className="text-cyan-400 inline-block w-8 drop-shadow-[0_0_5px_currentColor]">J</b> PUNCH</div>
           <div><b className="text-cyan-400 inline-block w-8 drop-shadow-[0_0_5px_currentColor]">W/S</b> JUMP/CROUCH</div>
           <div><b className="text-cyan-400 inline-block w-8 drop-shadow-[0_0_5px_currentColor]">K</b> KICK</div>
           <div></div>
           <div><b className="text-cyan-400 inline-block w-8 drop-shadow-[0_0_5px_currentColor]">L</b> BLOCK</div>
        </div>

        {/* Mobile touch controls */}
        <div className="md:hidden flex-1 flex justify-between gap-2 pointer-events-auto items-end pb-2 sm:gap-4 sm:pb-4 sm:px-2">
          {/* D-Pad */}
          <div data-testid="touch-dpad" className="grid h-[9.5rem] w-[9.5rem] grid-cols-3 gap-2 sm:h-44 sm:w-44 sm:gap-3">
            <div />
            <TouchButton action="jump" bind={onTouchStart} unbind={onTouchEnd} icon={<ChevronUp className="h-8 w-8 sm:h-10 sm:w-10" />} color="border-cyan-400 text-cyan-400" />
            <div />
            <TouchButton action="move_left" bind={onTouchStart} unbind={onTouchEnd} icon={<ChevronLeft className="h-8 w-8 sm:h-10 sm:w-10" />} color="border-cyan-400 text-cyan-400" />
            <TouchButton action="crouch" bind={onTouchStart} unbind={onTouchEnd} icon={<ChevronDown className="h-8 w-8 sm:h-10 sm:w-10" />} color="border-cyan-400 text-cyan-400" />
            <TouchButton action="move_right" bind={onTouchStart} unbind={onTouchEnd} icon={<ChevronRight className="h-8 w-8 sm:h-10 sm:w-10" />} color="border-cyan-400 text-cyan-400" />
          </div>

          {/* Action buttons (diagonal arrangement) */}
          <div className="relative h-[9.5rem] w-[9.5rem] sm:h-44 sm:w-44">
            <div className="absolute right-0 top-0 h-16 w-16 sm:h-[4.5rem] sm:w-[4.5rem]">
               <TouchButton action="heavy_kick" bind={onTouchStart} unbind={onTouchEnd} label="K" color="border-fuchsia-500 text-fuchsia-500" />
            </div>
            <div className="absolute bottom-0 left-0 h-16 w-16 sm:h-[4.5rem] sm:w-[4.5rem]">
               <TouchButton action="light_punch" bind={onTouchStart} unbind={onTouchEnd} label="P" color="border-cyan-400 text-cyan-400" />
            </div>
            <div className="absolute bottom-0 right-0 h-16 w-16 sm:h-[4.5rem] sm:w-[4.5rem]">
               <TouchButton action="block" bind={onTouchStart} unbind={onTouchEnd} label="B" color="border-purple-500 text-purple-500" />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function TouchButton({ action, bind, unbind, icon, label, color = "border-primary text-primary", mt = "" }: any) {
  return (
    <button
      className={`w-full h-full border-[3px] ${color} bg-[#020202] shadow-[6px_6px_0px_rgba(0,0,0,0.8)] active:shadow-[2px_2px_0px_rgba(0,0,0,0.8)] active:translate-x-[4px] active:translate-y-[4px] transition-all flex items-center justify-center text-4xl font-arcade ${mt}`}
      onPointerDown={(e) => {
        e.preventDefault();
        e.currentTarget.setPointerCapture(e.pointerId);
        bind(action)();
      }}
      onPointerUp={(e) => { e.preventDefault(); unbind(action)(); }}
      onPointerLeave={(e) => { e.preventDefault(); unbind(action)(); }}
      onPointerCancel={(e) => { e.preventDefault(); unbind(action)(); }}
      onContextMenu={(e) => e.preventDefault()}
      data-testid={`touch-${action}`}
    >
      <div className="drop-shadow-[0_0_8px_currentColor]">{icon || label}</div>
    </button>
  );
}
