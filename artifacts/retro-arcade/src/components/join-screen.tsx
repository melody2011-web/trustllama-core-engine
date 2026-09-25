import { useState } from 'react';
import { useArcadeEngine } from '@/hooks/use-arcade-engine';
import { GAME2_ENVIRONMENT } from '@/lib/game2-routes';

export function JoinScreen() {
  const { connection, state, actions } = useArcadeEngine();
  const [name, setName] = useState('');
  const [testPassword, setTestPassword] = useState('');
  const [testMessage, setTestMessage] = useState('');

  // If connected and we have a player, the join screen should hide.
  if (connection.status === 'connected') return null;

  const isConnecting = connection.status === 'connecting';

  return (
    <div className="absolute inset-0 bg-background/90 z-50 flex items-center justify-center p-4 backdrop-blur-sm" data-testid="overlay-join">
      <div className="max-w-md w-full bg-card border-4 border-primary p-8 flex flex-col items-center text-center retro-shadow-primary relative overflow-hidden">
        
        {/* CRT Scanline effect localized to this box */}
        <div className="absolute inset-0 crt-overlay opacity-50" />
        
        <div className="relative z-10 w-full flex flex-col items-center">
          <h1 className="font-pixel text-4xl md:text-5xl text-primary text-glow-primary uppercase mb-2">
            TrustLlama
          </h1>
          <h2 className="font-pixel text-2xl md:text-3xl text-secondary text-glow-secondary uppercase mb-8">
            TLAMA Chomp
          </h2>

          <div className="w-full bg-black/50 border border-muted p-4 mb-8">
            <div className="flex justify-between font-mono text-sm mb-2 text-muted-foreground">
              <span>ENTRY FEE:</span>
              <span className="text-success font-bold">{state.simulated_entry_fee_tlama} TLAMA</span>
            </div>
            <div className="flex justify-between font-mono text-sm text-muted-foreground">
              <span>NETWORK STATUS:</span>
              <span className={connection.status === 'error' ? 'text-destructive' : 'text-accent'}>
                {connection.message}
              </span>
            </div>
          </div>

          {connection.status === 'error' ? (
            <div className="flex flex-col items-center gap-4 w-full">
              <p className="font-mono text-destructive uppercase font-bold" data-testid="text-error">
                Connection Failed
              </p>
              <button
                onClick={() => actions.reset()}
                className="w-full py-3 bg-destructive text-destructive-foreground font-pixel text-xl uppercase hover:bg-white hover:text-black transition-colors"
                data-testid="button-retry"
              >
                Retry Connection
              </button>
            </div>
          ) : (
            <form 
              onSubmit={(e) => {
                e.preventDefault();
                if (name.trim()) actions.join(name.trim());
              }}
              className="flex flex-col gap-4 w-full"
            >
              <div className="flex flex-col gap-2 text-left">
                <label htmlFor="player-name" className="font-mono text-primary uppercase text-sm font-bold">
                  Enter Initials
                </label>
                <input
                  id="player-name"
                  type="text"
                 maxLength={24}
                  value={name}
                  onChange={(e) => setName(e.target.value.toUpperCase())}
                  disabled={isConnecting}
                  className="bg-black border-2 border-muted p-3 font-mono text-2xl text-center text-white uppercase focus:border-primary focus:outline-none placeholder:text-muted-foreground/50 transition-colors"
                  placeholder="AAA"
                  data-testid="input-player-name"
                  autoComplete="off"
                />
              </div>

              <button
                type="submit"
                disabled={isConnecting || !name.trim()}
                className="mt-4 w-full py-4 bg-primary text-primary-foreground font-pixel text-2xl uppercase hover:bg-white hover:text-black disabled:opacity-50 disabled:cursor-not-allowed transition-all hover:retro-shadow"
                data-testid="button-join"
              >
                {isConnecting ? 'INSERTING COIN...' : 'INSERT COIN'}
              </button>
            </form>
          )}

          {GAME2_ENVIRONMENT.developerControlsEnabled && connection.status !== 'error' ? (
            <details className="mt-6 w-full border border-secondary/40 bg-black/50 p-3 text-left">
              <summary className="cursor-pointer select-none font-mono text-xs font-bold uppercase tracking-widest text-secondary">
                Developer Test Portal
              </summary>
              <form
                className="mt-4 flex flex-col gap-3"
                onSubmit={(event) => event.preventDefault()}
              >
                <input
                  type="text"
                  name="username"
                  value={name}
                  autoComplete="username"
                  readOnly
                  tabIndex={-1}
                  aria-hidden="true"
                  className="sr-only"
                />
                <p className="font-mono text-[11px] leading-relaxed text-muted-foreground">
                  Authenticated R&amp;D only. The server resets and confirms the selected level.
                </p>
                <label htmlFor="test-password" className="font-mono text-xs font-bold uppercase text-primary">
                  Operator password
                </label>
                <input
                  id="test-password"
                  type="password"
                  value={testPassword}
                  onChange={(event) => setTestPassword(event.target.value)}
                  autoComplete="current-password"
                  className="border border-muted bg-black p-2 font-mono text-sm text-white focus:border-secondary focus:outline-none"
                />
                <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
                  {([1, 2, 3] as const).map((level) => (
                    <button
                      key={level}
                      type="button"
                      disabled={isConnecting || !name.trim() || !testPassword}
                      onClick={async () => {
                        const password = testPassword;
                        setTestPassword('');
                        setTestMessage(`Requesting Level ${level}…`);
                        try {
                          await actions.joinTestLevel(name.trim(), level, password);
                          setTestMessage(`Level ${level} requested from server.`);
                        } catch (error) {
                          setTestMessage(error instanceof Error ? error.message : 'Test request failed.');
                        }
                      }}
                      className="min-h-11 border border-secondary bg-secondary/10 px-2 font-pixel text-sm uppercase text-secondary transition-colors hover:bg-secondary hover:text-black disabled:cursor-not-allowed disabled:opacity-40"
                      data-testid={`button-test-level-${level}`}
                    >
                      Test Level {level}
                    </button>
                  ))}
                </div>
                {testMessage ? (
                  <p className="font-mono text-[11px] text-accent" aria-live="polite">
                    {testMessage}
                  </p>
                ) : null}
              </form>
            </details>
          ) : null}

          <div className="mt-8 font-mono text-xs text-muted-foreground flex gap-4">
            <span className="animate-pulse">▶ PRESS START ◀</span>
          </div>
        </div>
      </div>
    </div>
  );
}
