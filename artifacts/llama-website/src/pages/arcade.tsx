import { useEffect, useMemo } from 'react';
import { Gamepad2, AlertTriangle, ArrowLeft, User, Ticket, History, Shield, RefreshCcw, CheckCircle2, XCircle, Lock, Trophy } from 'lucide-react';
import { Link } from 'wouter';
import { useGetArcadeDashboard, useHealthCheck } from '@workspace/api-client-react';

const PLAYER_KEY_STORAGE = 'tlama-arcade-player-key';
const OWNERSHIP_TOKEN_STORAGE = 'tlama-arcade-ownership-token';
const EXAMPLE_MATCHES = [
  {
    id: 'example-champion',
    score: 2_480,
    date: 'Example · 18 Aug 2026',
    outcome: '1st place',
    reward: '+120 TLAMA',
    accent: 'text-emerald-700 dark:text-emerald-400',
  },
  {
    id: 'example-runner-up',
    score: 1_760,
    date: 'Example · 14 Aug 2026',
    outcome: 'Top 10',
    reward: '+45 TLAMA',
    accent: 'text-primary',
  },
  {
    id: 'example-complete',
    score: 890,
    date: 'Example · 09 Aug 2026',
    outcome: 'Completed',
    reward: 'No reward',
    accent: 'text-muted-foreground',
  },
] as const;
const EXAMPLE_TICKET_PACKAGES = [
  {
    id: 'single-pass',
    name: 'Single Pass',
    passes: 1,
    cost: 25,
    description: 'One example entry for a standard TLAMA match.',
    featured: false,
  },
  {
    id: 'five-pack',
    name: 'Player Pack',
    passes: 5,
    cost: 115,
    description: 'Five example match entries grouped into one bundle.',
    featured: true,
  },
  {
    id: 'ten-pack',
    name: 'Arcade Bundle',
    passes: 10,
    cost: 220,
    description: 'Ten example entries for regular TLAMA players.',
    featured: false,
  },
] as const;

function readArcadeIdentity() {
  try {
    const fragmentPrefix = '#arcade-identity=';
    if (window.location.hash.startsWith(fragmentPrefix)) {
      const encoded = window.location.hash.slice(fragmentPrefix.length);
      const padded = encoded
        .replace(/-/g, '+')
        .replace(/_/g, '/')
        .padEnd(Math.ceil(encoded.length / 4) * 4, '=');
      const parsed = JSON.parse(atob(padded)) as {
        playerKey?: unknown;
        ownershipToken?: unknown;
      };
      if (
        typeof parsed.playerKey === 'string' &&
        parsed.playerKey.length >= 32 &&
        parsed.playerKey.length <= 36 &&
        typeof parsed.ownershipToken === 'string' &&
        parsed.ownershipToken.length >= 32 &&
        parsed.ownershipToken.length <= 128
      ) {
        window.history.replaceState(
          null,
          '',
          `${window.location.pathname}${window.location.search}`,
        );
        try {
          window.localStorage.setItem(PLAYER_KEY_STORAGE, parsed.playerKey);
          window.localStorage.setItem(
            OWNERSHIP_TOKEN_STORAGE,
            parsed.ownershipToken,
          );
        } catch {
          // The transferred identity remains usable for this page load.
        }
        return {
          playerKey: parsed.playerKey,
          ownershipToken: parsed.ownershipToken,
        };
      }
    }
    const playerKey = window.localStorage.getItem(PLAYER_KEY_STORAGE);
    const ownershipToken = window.localStorage.getItem(OWNERSHIP_TOKEN_STORAGE);
    return playerKey && ownershipToken ? { playerKey, ownershipToken } : null;
  } catch {
    return null;
  }
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(new Date(value));
}

function SystemStatus() {
  const { data: health, isLoading, isError, refetch } = useHealthCheck();

  return (
    <div className="bg-card border border-border shadow-sm rounded-2xl p-5 mb-8 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
      <div className="flex items-center gap-4">
        <div className="w-12 h-12 rounded-xl bg-muted/50 border border-border/50 flex items-center justify-center">
          <ActivityIcon isLoading={isLoading} isError={isError} isSuccess={!!health} />
        </div>
        <div>
          <h3 className="font-display font-bold text-lg">Backend Connectivity</h3>
          <p className="text-sm text-muted-foreground">
            {isLoading && 'Connecting to game services...'}
            {isError && 'Connection to services failed.'}
            {health && `Status: ${health.status === 'ok' ? 'Online & Healthy' : health.status}`}
          </p>
        </div>
      </div>
      <button 
        onClick={() => refetch()}
        disabled={isLoading}
        className="shrink-0 flex items-center gap-2 px-4 py-2 bg-secondary/10 text-secondary hover:bg-secondary/20 transition-colors rounded-full font-bold text-sm disabled:opacity-50"
      >
        <RefreshCcw className={`w-4 h-4 ${isLoading ? 'animate-spin' : ''}`} />
        Retry Connection
      </button>
    </div>
  );
}

function ActivityIcon({ isLoading, isError, isSuccess }: { isLoading: boolean, isError: boolean, isSuccess: boolean }) {
  if (isLoading) return <RefreshCcw className="w-6 h-6 text-muted-foreground animate-spin" />;
  if (isError) return <XCircle className="w-6 h-6 text-destructive" />;
  if (isSuccess) return <CheckCircle2 className="w-6 h-6 text-emerald-500" />;
  return <AlertTriangle className="w-6 h-6 text-orange-500" />;
}

export function Arcade() {
  const identity = useMemo(readArcadeIdentity, []);
  const dashboardQuery = useGetArcadeDashboard({
    request: {
      headers: identity
        ? {
            'X-Tlama-Player-Key': identity.playerKey,
            'X-Tlama-Ownership-Token': identity.ownershipToken,
          }
        : undefined,
    },
    query: {
      enabled: identity !== null,
      queryKey: ['/api/arcade/dashboard', identity?.playerKey ?? 'guest'],
    },
  });
  const dashboard = dashboardQuery.data;
  const dashboardErrorStatus = dashboardQuery.error?.status;

  useEffect(() => {
    const previousTitle = document.title;
    document.title = 'TLAMA Dashboard | TrustLlama';
    const meta = document.querySelector('meta[name="description"]');
    const previousDescription = meta?.getAttribute('content') ?? null;
    if (meta) {
      meta.setAttribute('content', 'Preview the TLAMA player dashboard. Honest gaming with transparent limits.');
    }

    return () => {
      document.title = previousTitle;
      if (meta && previousDescription !== null) {
        meta.setAttribute('content', previousDescription);
      }
    };
  }, []);

  return (
    <div className="min-h-[100dvh] bg-background flex flex-col relative overflow-hidden text-foreground">
      {/* Background Decor - game themed but subtle */}
      <div className="absolute top-[-20%] right-[-10%] w-[50vw] h-[50vw] rounded-full bg-primary/5 blur-[120px] pointer-events-none" />
      <div className="absolute bottom-[-10%] left-[-10%] w-[40vw] h-[40vw] rounded-full bg-secondary/5 blur-[100px] pointer-events-none" />
      
      {/* Navigation */}
      <nav className="w-full border-b border-border/50 bg-background/80 backdrop-blur-xl sticky top-0 z-50">
        <div className="max-w-7xl mx-auto px-6 h-20 flex items-center justify-between">
          <div className="flex items-center gap-4">
            <Link href="/" aria-label="Back to TrustLlama home" className="w-10 h-10 flex items-center justify-center text-muted-foreground hover:text-foreground hover:bg-muted rounded-full transition-colors" data-testid="link-back">
              <ArrowLeft className="w-5 h-5" />
            </Link>
            <div className="flex items-center gap-3 border-l border-border/50 pl-4">
              <div className="w-8 h-8 rounded-lg bg-primary text-primary-foreground flex items-center justify-center shadow-md">
                <Gamepad2 className="w-4 h-4" />
              </div>
              <span className="font-display font-bold text-xl tracking-tight">TLAMA</span>
            </div>
          </div>
          <div className="flex items-center">
            <div className="flex items-center gap-2 px-3 py-1.5 rounded-full bg-orange-500/10 text-orange-600 dark:text-orange-400 border border-orange-500/20 shadow-sm" data-testid="status-offline">
              <AlertTriangle className="w-3.5 h-3.5" />
              <span className="text-xs font-bold uppercase tracking-widest">Read-Only</span>
            </div>
          </div>
        </div>
      </nav>

      <main className="flex-1 w-full max-w-7xl mx-auto px-6 py-12">
        <div className="mb-10 animate-fade-up opacity-0" style={{ animationDelay: '100ms' }}>
          <h1 className="text-4xl md:text-5xl font-display font-bold tracking-tight mb-4">Player Dashboard</h1>
            <p className="text-xl text-muted-foreground max-w-2xl">
               A transparent view of your saved TLAMA profile, simulated tickets, and completed matches. Payments remain disabled.
          </p>
        </div>

        <div className="animate-fade-up opacity-0" style={{ animationDelay: '200ms' }}>
          <SystemStatus />
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
          {/* Left Column - Profile & Navigation */}
          <div className="flex flex-col gap-8 animate-fade-up opacity-0" style={{ animationDelay: '300ms' }}>
            {/* Player Profile Card */}
            <div className="bg-card border border-border shadow-sm rounded-3xl p-6 relative overflow-hidden" data-testid="card-profile">
              <div className="absolute top-0 right-0 w-32 h-32 bg-primary/10 rounded-full blur-3xl" />
              <div className="flex items-start justify-between mb-6 relative z-10">
                <div className="w-16 h-16 rounded-2xl bg-muted/80 border-2 border-border flex items-center justify-center shadow-inner">
                  <User className="w-8 h-8 text-muted-foreground" />
                </div>
                <div className={`px-3 py-1 rounded-full text-xs font-bold uppercase tracking-widest flex items-center gap-1.5 ${dashboard ? 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-400' : 'bg-muted text-muted-foreground'}`}>
                  <Shield className="w-3 h-3" /> {dashboard ? 'Verified Player' : 'Guest Mode'}
                </div>
              </div>
              
              <div className="relative z-10">
                <h2 className="text-2xl font-display font-bold mb-1">
                  {dashboardQuery.isLoading ? 'Loading player…' : dashboard?.player.name ?? 'No Saved Player'}
                </h2>
                <p className="text-sm text-muted-foreground mb-6">
                  {dashboard
                    ? `Arcade ID ${dashboard.player.playerId.slice(0, 14)}…`
                    : !identity
                      ? 'Join the multiplayer preview to create a browser identity.'
                      : dashboardErrorStatus === 401
                        ? 'This browser could not prove ownership of the saved identity.'
                        : 'No saved Arcade profile was found for this browser.'}
                </p>
                
                <div className="grid grid-cols-2 gap-4">
                  <div className="bg-muted/30 border border-border/50 rounded-xl p-4">
                    <div className="text-xs text-muted-foreground font-bold uppercase tracking-widest mb-1">High Score</div>
                    <div className="text-lg font-display font-bold">{dashboard?.player.highScore ?? '--'}</div>
                  </div>
                  <div className="bg-muted/30 border border-border/50 rounded-xl p-4">
                    <div className="text-xs text-muted-foreground font-bold uppercase tracking-widest mb-1">Matches</div>
                    <div className="text-lg font-display font-bold">{dashboard?.player.matchesPlayed ?? '--'}</div>
                  </div>
                </div>
                
                <button
                  type="button"
                  onClick={() => dashboardQuery.refetch()}
                  disabled={!identity || dashboardQuery.isFetching}
                  className="w-full mt-6 py-3 px-4 bg-primary/10 text-primary border border-primary/20 rounded-xl font-bold hover:bg-primary/20 transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2"
                >
                  <RefreshCcw className={`w-4 h-4 ${dashboardQuery.isFetching ? 'animate-spin' : ''}`} />
                  Refresh Player Data
                </button>
              </div>
            </div>
            
            {/* Trust Notice */}
            <div className="bg-gradient-to-br from-foreground to-foreground/90 text-background rounded-3xl p-6">
              <h3 className="font-display font-bold text-lg mb-2 flex items-center gap-2">
                <CheckCircle2 className="w-5 h-5 text-primary" /> Transparent Gaming
              </h3>
              <p className="text-background/70 text-sm leading-relaxed">
                  TLAMA mechanics are deterministic. Reward allocation is an offline preview policy only: no payout entitlement, settlement, or TLAMA transfer is active. We do not use hidden algorithms to alter game difficulty.
              </p>
            </div>
          </div>

          {/* Right Column - Main Content */}
          <div className="lg:col-span-2 flex flex-col gap-8 animate-fade-up opacity-0" style={{ animationDelay: '400ms' }}>
            
            {/* Game Tickets */}
            <section data-testid="section-tickets">
              <div className="flex items-center gap-2 text-sm font-bold uppercase tracking-widest text-primary mb-4">
                <Ticket className="w-4 h-4" /> Available Tickets
              </div>
              
              {dashboard && dashboard.activeTickets.length > 0 ? (
                <div className="grid gap-3">
                  {dashboard.activeTickets.map((ticket) => (
                    <article key={ticket.ticketId} className="bg-card border border-primary/20 shadow-sm rounded-2xl p-5 flex flex-col sm:flex-row sm:items-center justify-between gap-4">
                      <div className="flex items-center gap-4">
                        <div className="w-12 h-12 rounded-xl bg-primary/10 text-primary flex items-center justify-center">
                          <Ticket className="w-6 h-6" />
                        </div>
                        <div>
                          <h3 className="font-display font-bold">Simulated match ticket</h3>
                          <p className="text-sm text-muted-foreground">Issued {formatDate(ticket.issuedAt)}</p>
                        </div>
                      </div>
                      <div className="sm:text-right">
                        <div className="font-display font-bold">{ticket.entryFeeTlama} TLAMA</div>
                        <div className="text-xs uppercase tracking-widest text-primary font-bold">Active simulation</div>
                      </div>
                    </article>
                  ))}
                </div>
              ) : (
                dashboardQuery.isLoading ? (
                  <div className="bg-card border border-border border-dashed shadow-sm rounded-3xl p-10 text-center flex flex-col items-center justify-center min-h-[240px]">
                    <RefreshCcw className="w-10 h-10 text-muted-foreground/40 animate-spin mb-4" />
                    <p className="font-medium text-muted-foreground">Checking this browser for active simulated tickets…</p>
                  </div>
                ) : (
                  <div className="bg-card border border-border shadow-sm rounded-3xl overflow-hidden">
                    <div className="px-6 py-5 bg-primary/5 border-b border-primary/15 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
                      <div>
                        <div className="flex items-center gap-2 text-primary font-bold">
                          <AlertTriangle className="w-4 h-4" />
                          Illustrative ticket catalog
                        </div>
                        <p className="mt-1 text-sm text-muted-foreground">
                          Example packages and costs only. Tickets are not for sale and no TLAMA can be transferred.
                        </p>
                      </div>
                      <span className="shrink-0 self-start rounded-full border border-primary/20 bg-background px-3 py-1 text-xs font-bold uppercase tracking-widest text-primary">
                        Demo data
                      </span>
                    </div>
                    <div className="grid gap-4 p-5 md:grid-cols-3">
                      {EXAMPLE_TICKET_PACKAGES.map((ticket) => (
                        <article
                          key={ticket.id}
                          className={`relative flex flex-col rounded-2xl border p-5 ${
                            ticket.featured
                              ? 'border-primary/35 bg-gradient-to-b from-primary/10 to-card shadow-lg shadow-primary/5'
                              : 'border-border bg-muted/10'
                          }`}
                        >
                          {ticket.featured && (
                            <span className="absolute right-4 top-4 rounded-full bg-primary px-2.5 py-1 text-[0.6rem] font-bold uppercase tracking-widest text-primary-foreground">
                              Example bundle
                            </span>
                          )}
                          <div className={`mb-5 flex h-12 w-12 items-center justify-center rounded-xl ${ticket.featured ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground'}`}>
                            <Ticket className="h-6 w-6" />
                          </div>
                          <p className="text-xs font-bold uppercase tracking-widest text-muted-foreground">
                            {ticket.passes} {ticket.passes === 1 ? 'entry' : 'entries'}
                          </p>
                          <h3 className="mt-2 text-xl font-display font-bold">{ticket.name}</h3>
                          <p className="mt-2 min-h-10 text-sm leading-relaxed text-muted-foreground">{ticket.description}</p>
                          <div className="my-5 border-t border-border/60" />
                          <div className="flex items-end gap-2">
                            <span className="text-3xl font-display font-bold">{ticket.cost}</span>
                            <span className="pb-1 text-sm font-bold text-muted-foreground">TLAMA</span>
                          </div>
                          <p className="mt-1 text-[0.65rem] font-bold uppercase tracking-widest text-secondary">Example cost · not charged</p>
                          <button
                            type="button"
                            disabled
                            className="mt-5 flex w-full cursor-not-allowed items-center justify-center gap-2 rounded-xl border border-border bg-muted px-4 py-2.5 text-sm font-bold text-muted-foreground opacity-70"
                          >
                            <Lock className="h-4 w-4" />
                            Preview only
                          </button>
                        </article>
                      ))}
                    </div>
                    <div className="border-t border-border/60 bg-muted/20 px-6 py-4 text-center text-xs font-medium text-muted-foreground">
                      No checkout, wallet signing, payment settlement, or reward entitlement is active.
                    </div>
                  </div>
                )
              )}
            </section>

            {/* Match History */}
            <section data-testid="section-history">
              <div className="flex items-center gap-2 text-sm font-bold uppercase tracking-widest text-secondary mb-4">
                <History className="w-4 h-4" /> Match History
              </div>
              
              <div className="bg-card border border-border shadow-sm rounded-3xl overflow-hidden">
                {dashboard && dashboard.matchHistory.length > 0 ? (
                  <div className="divide-y divide-border/50">
                    {dashboard.matchHistory.map((match) => (
                      <article key={match.ticketId} className="p-6 flex items-center justify-between gap-4">
                      <div className="flex items-center gap-4">
                          <div className="w-12 h-12 rounded-xl bg-secondary/10 text-secondary flex items-center justify-center">
                            <Trophy className="w-6 h-6" />
                          </div>
                          <div>
                            <h3 className="font-display font-bold">Completed match</h3>
                            <p className="text-sm text-muted-foreground">{formatDate(match.completedAt)}</p>
                          </div>
                        </div>
                        <div className="text-right">
                          <div className="text-xl font-display font-bold">{match.finalScore}</div>
                          <div className="text-xs uppercase tracking-widest text-muted-foreground font-bold">Final score</div>
                        </div>
                      </article>
                    ))}
                  </div>
                ) : (
                  dashboardQuery.isLoading ? (
                    <div className="p-10 text-center">
                      <RefreshCcw className="w-10 h-10 mx-auto text-muted-foreground/40 mb-4 animate-spin" />
                      <p className="font-medium text-muted-foreground">Loading match history…</p>
                    </div>
                  ) : (
                    <div>
                      <div className="px-6 py-5 bg-secondary/5 border-b border-secondary/15 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
                        <div>
                          <div className="flex items-center gap-2 text-secondary font-bold">
                            <AlertTriangle className="w-4 h-4" />
                            Illustrative preview
                          </div>
                          <p className="mt-1 text-sm text-muted-foreground">
                            Example layout only. These matches did not occur and no TLAMA rewards were paid.
                          </p>
                        </div>
                        <span className="shrink-0 self-start rounded-full border border-secondary/20 bg-background px-3 py-1 text-xs font-bold uppercase tracking-widest text-secondary">
                          Demo data
                        </span>
                      </div>
                      <div className="divide-y divide-border/50">
                        {EXAMPLE_MATCHES.map((match, index) => (
                          <article key={match.id} className="p-5 sm:p-6 flex items-center justify-between gap-4 relative overflow-hidden">
                            <div className="absolute inset-y-0 left-0 w-1 bg-gradient-to-b from-primary to-secondary opacity-60" />
                            <div className="flex items-center gap-4 min-w-0">
                              <div className="w-12 h-12 shrink-0 rounded-xl bg-gradient-to-br from-primary/15 to-secondary/10 text-primary flex items-center justify-center font-display font-bold">
                                {String(index + 1).padStart(2, '0')}
                              </div>
                              <div className="min-w-0">
                                <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                                  <h3 className="font-display font-bold">{match.score.toLocaleString()} points</h3>
                                  <span className="rounded-full bg-muted px-2.5 py-1 text-[0.65rem] font-bold uppercase tracking-widest text-muted-foreground">
                                    {match.outcome}
                                  </span>
                                </div>
                                <p className="mt-1 text-sm text-muted-foreground">{match.date}</p>
                              </div>
                            </div>
                            <div className="shrink-0 text-right">
                              <div className={`text-lg font-display font-bold ${match.accent}`}>{match.reward}</div>
                              <div className="mt-1 text-[0.65rem] uppercase tracking-widest text-muted-foreground font-bold">Not paid</div>
                            </div>
                          </article>
                        ))}
                      </div>
                    </div>
                  )
                )}
              </div>
            </section>
            
          </div>
        </div>
      </main>
    </div>
  );
}
