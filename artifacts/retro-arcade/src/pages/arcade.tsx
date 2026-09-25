import { Playfield } from '@/components/playfield';
import { HUD, StageSelector } from '@/components/hud';
import { TouchControls } from '@/components/controls';
import { JoinScreen } from '@/components/join-screen';
import { StatusOverlay } from '@/components/status-overlay';
import { useArcadeEngine } from '@/hooks/use-arcade-engine';
import { FeedbackPanel } from '@/components/feedback-panel';

export default function ArcadePage() {
  const { state } = useArcadeEngine();
  return (
    <div className="absolute inset-0 w-full h-full bg-black overflow-y-auto overflow-x-hidden">
      <div className="min-h-full flex flex-col items-center justify-center py-4 md:py-8 px-2 md:px-8">
        {/* Outer Cabinet Shell */}
        <div className="relative w-full max-w-5xl flex flex-col p-4 md:p-8 bg-zinc-950 md:border-[16px] md:border-zinc-900 md:rounded-3xl shadow-2xl shrink-0 my-auto">

          {/* Cabinet Marquee Header (Hidden on small mobile) */}
          <div className="hidden md:flex justify-between items-center mb-4 px-6 py-2 bg-gradient-to-b from-primary/20 to-transparent border-b border-primary/30">
            <div className="font-pixel text-primary text-2xl tracking-widest text-glow-primary">
              TRUSTLLAMA
            </div>
            <div className="flex gap-2">
              <div className="w-3 h-3 rounded-full bg-destructive animate-pulse"></div>
              <div className="w-3 h-3 rounded-full bg-accent"></div>
            </div>
          </div>

          {/* The Screen Bezel */}
          <div className="relative w-full h-[50vh] md:h-[65vh] min-h-[400px] max-h-[700px] flex-grow bg-black rounded-lg md:rounded-xl overflow-hidden border-4 md:border-8 border-zinc-800 shadow-[inset_0_0_50px_rgba(0,0,0,1)] shrink-0">

            <Playfield />
            <HUD />
            <TouchControls />
            <StatusOverlay />
            <JoinScreen />

            {/* Permanent Screen Glare/Reflection */}
            <div className="absolute inset-0 bg-gradient-to-tr from-white/5 via-transparent to-white/10 pointer-events-none z-[60]" />
          </div>

          <StageSelector />

          {/* Cabinet Coin Slot Area (Hidden on small mobile) */}
          <div className="hidden md:flex justify-center mt-6 shrink-0">
            <div className="bg-zinc-900 border-2 border-zinc-700 px-8 py-4 rounded shadow-[inset_0_4px_10px_rgba(0,0,0,0.5)] flex items-center gap-4">
              <div className="w-2 h-8 bg-black rounded-full border border-zinc-600"></div>
              <div className="font-mono text-accent text-xs uppercase tracking-widest text-glow-accent">
                Insert {state.simulated_entry_fee_tlama} TLAMA
              </div>
              <div className="w-2 h-8 bg-black rounded-full border border-zinc-600"></div>
            </div>
          </div>

          <FeedbackPanel />
        </div>
      </div>
    </div>
  );
}
