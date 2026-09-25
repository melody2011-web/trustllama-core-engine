import { Link } from 'wouter';
import {
  Shield, Lock, Ban, Activity, ArrowRight, CheckCircle2, ChevronRight,
  FileCode2, Database, AlertTriangle, MonitorPlay, BookOpen,
  LayoutDashboard, Gamepad2, TerminalSquare, Swords, Download
} from 'lucide-react';
import {
  SiDiscord,
  SiLinktree,
  SiTelegram,
  SiTiktok,
  SiX,
} from 'react-icons/si';
import profileLogo from '../assets/trustllama-profile-logo.svg';

const communityLinks = [
  { label: 'Telegram', href: 'https://t.me/trustllama', icon: SiTelegram },
  { label: 'Discord', href: 'https://discord.gg/m3zShzf2F', icon: SiDiscord },
  { label: 'Linktree', href: 'https://linktr.ee/trustllama2026', icon: SiLinktree },
  { label: 'TikTok', href: 'https://www.tiktok.com/@trustllama?lang=en', icon: SiTiktok },
  { label: 'X', href: 'https://x.com/TrustLlama', icon: SiX },
] as const;

export function Landing() {
  const multiplayerGameUrl = '/llama-website/game/';
  const retroArcadeUrl = '/retro-arcade/';
  const whitepaperUrl = `${import.meta.env.BASE_URL}whitepaper.html`;

  return (
    <div className="min-h-[100dvh] bg-background flex flex-col relative overflow-hidden text-foreground">
      {/* Background Decor */}
      <div className="absolute top-[-20%] left-[-10%] w-[70vw] h-[70vw] rounded-full bg-primary/10 blur-[100px] pointer-events-none" />
      <div className="absolute bottom-[-20%] right-[-10%] w-[60vw] h-[60vw] rounded-full bg-secondary/10 blur-[100px] pointer-events-none" />

      {/* Navigation */}
      <nav className="w-full border-b border-border/50 bg-background/80 backdrop-blur-xl sticky top-0 z-50">
        <div className="max-w-7xl mx-auto px-6 h-20 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <img src={profileLogo} alt="TrustLlama logo" width="40" height="40" className="w-10 h-10 rounded-xl shadow-lg" />
            <span className="hidden font-display font-bold text-2xl tracking-tight text-foreground sm:inline">TrustLlama</span>
          </div>
          <div className="hidden md:flex items-center gap-8">
            <a href="#manifesto" className="text-sm font-bold tracking-wide text-muted-foreground hover:text-foreground transition-colors" data-testid="nav-link-manifesto">Manifesto</a>
            <a href="#rules" className="text-sm font-bold tracking-wide text-muted-foreground hover:text-foreground transition-colors" data-testid="nav-link-rules">Immutable Rules</a>
            <a href="#verification" className="text-sm font-bold tracking-wide text-muted-foreground hover:text-foreground transition-colors" data-testid="nav-link-verification">Verification</a>
          </div>
          <div className="flex items-center gap-4">
            <Link
              href="/buy"
              className="inline-flex items-center justify-center rounded-full bg-blue-600 px-4 py-2 text-xs font-bold text-white shadow-lg shadow-blue-600/25 transition-all hover:-translate-y-0.5 hover:bg-blue-700 sm:px-5 sm:text-sm"
              data-testid="nav-btn-commercial-licenses"
            >
              Browse Commercial Licenses
            </Link>
            <div className="hidden lg:flex items-center gap-2 px-4 py-2 rounded-full bg-secondary/15 text-secondary border border-secondary/20 shadow-sm">
              <span className="w-2 h-2 rounded-full bg-secondary animate-pulse" />
              <span className="text-xs font-bold uppercase tracking-widest">Pre-Launch</span>
            </div>
            <a href={multiplayerGameUrl} target="_blank" rel="noreferrer" className="hidden lg:flex items-center gap-2 px-4 py-2 rounded-full bg-primary text-primary-foreground font-bold text-sm hover:bg-primary/90 transition-colors shadow-sm shadow-primary/20" data-testid="nav-btn-play">
              <MonitorPlay className="w-4 h-4" /> Play Arcade
            </a>
          </div>
        </div>
      </nav>

      <main className="flex-1 w-full max-w-7xl mx-auto px-6 pt-20 pb-32">
        {/* Status Alert */}
        <div className="mb-16 animate-fade-up opacity-0 flex justify-center md:justify-start" style={{ animationDelay: '100ms' }}>
          <div className="inline-flex items-center gap-3 px-5 py-2.5 rounded-full border border-primary/30 bg-primary/10 text-primary">
            <CheckCircle2 className="w-4 h-4" />
            <span className="text-xs sm:text-sm font-bold tracking-wider uppercase" data-testid="hero-status-alert">STATUS: ARCHITECTURE VERIFIED • PRICE FEEDS ACTIVE</span>
          </div>
        </div>

        {/* Hero Section */}
        <div id="manifesto" className="max-w-4xl relative z-10 scroll-m-28">
          <h1 className="max-w-full text-[2.5rem] sm:text-6xl md:text-7xl lg:text-[5.5rem] font-display font-extrabold leading-[1.08] sm:leading-[1.05] tracking-tight animate-fade-up opacity-0" style={{ animationDelay: '200ms' }}>
            TrustLlama: The <span className="text-transparent bg-clip-text bg-gradient-to-r from-primary to-blue-400">Secure, Cheat-Proof</span>{' '}
            Server-Authoritative Web3 Arcade Framework.
          </h1>
          <Link
            href="/buy"
            className="mt-8 inline-flex w-full items-center justify-center gap-2 rounded-full bg-blue-600 px-8 py-4 text-center text-base font-bold text-white shadow-xl shadow-blue-600/30 transition-all hover:scale-[1.02] hover:bg-blue-700 sm:w-auto sm:text-lg group animate-fade-up opacity-0"
            style={{ animationDelay: '300ms' }}
            data-testid="hero-btn-commercial-licenses"
          >
            👉 View Enterprise Pricing &amp; Instant USDC Checkout
            <ArrowRight className="w-5 h-5 shrink-0 group-hover:translate-x-1 transition-transform" />
          </Link>
          <p className="mt-8 text-xl md:text-2xl text-muted-foreground max-w-2xl leading-relaxed animate-fade-up opacity-0 font-medium" style={{ animationDelay: '300ms' }}>
            TRUSTLLAMA LTD licenses a 30Hz FastAPI/WebSocket server-authoritative arcade framework with local SQLite Write-Ahead Logging (WAL) persistence. Server-owned gameplay logic and durable state are the focus of the commercial packages.
          </p>

          <div className="mt-12 flex flex-col sm:flex-row flex-wrap items-center gap-4 animate-fade-up opacity-0" style={{ animationDelay: '400ms' }}>
            <a href={multiplayerGameUrl} target="_blank" rel="noreferrer" className="w-full sm:w-auto px-8 py-4 bg-primary text-primary-foreground rounded-full font-bold text-lg hover:bg-primary/90 hover:scale-105 transition-all flex items-center justify-center gap-2 shadow-xl shadow-primary/25 group" data-testid="hero-btn-play">
              <MonitorPlay className="w-5 h-5" />
              Play 10-Level Arcade
              <ArrowRight className="w-5 h-5 group-hover:translate-x-1 transition-transform" />
            </a>
            <a href={whitepaperUrl} target="_blank" rel="noreferrer" className="w-full sm:w-auto px-8 py-4 bg-card border-2 border-border text-foreground rounded-full font-bold text-lg hover:border-foreground/30 hover:bg-muted/50 transition-all flex items-center justify-center gap-2" data-testid="hero-btn-whitepaper">
              <BookOpen className="w-5 h-5" />
              View White Paper
            </a>
            <Link href="/technical-evidence" className="w-full sm:w-auto px-8 py-4 bg-card border-2 border-border text-foreground rounded-full font-bold text-lg hover:border-foreground/30 hover:bg-muted/50 transition-all flex items-center justify-center gap-2" data-testid="hero-btn-technical-evidence">
              <FileCode2 className="w-5 h-5" />
              Technical Evidence
            </Link>
            {import.meta.env.DEV ? (
              <div className="flex w-full flex-col gap-3 sm:w-auto">
                <p className="text-sm text-muted-foreground">Operator-only downloads. Use username <strong>operator</strong> and your separate download password when prompted.</p>
                <a
                  href="/trustllama-core-backend-v44.zip"
                  target="_blank"
                  rel="noreferrer"
                  className="w-full sm:w-auto px-8 py-4 bg-foreground text-background rounded-full font-bold text-lg hover:opacity-90 transition-all flex items-center justify-center gap-2 shadow-xl shadow-black/10"
                  data-testid="hero-btn-backend-release"
                >
                  <Download className="w-5 h-5" />
                  Download Backend v44
                </a>
                <a
                  href="/solana-webhook-core-rail-pro.zip"
                  className="w-full sm:w-auto px-8 py-4 bg-foreground text-background rounded-full font-bold text-lg hover:opacity-90 transition-all flex items-center justify-center gap-2 shadow-xl shadow-black/10"
                  data-testid="hero-btn-webhook-pro-asset"
                >
                  <Download className="w-5 h-5" />
                  Download Webhook Pro Asset
                </a>
              </div>
            ) : null}
          </div>

          <div className="mt-8 flex flex-wrap items-center gap-3 animate-fade-up opacity-0" style={{ animationDelay: '500ms' }}>
            <Link href="/arcade" className="px-5 py-2.5 rounded-full border border-border bg-background/50 hover:bg-muted text-sm font-semibold flex items-center gap-2 transition-colors" data-testid="hero-btn-dashboard">
              <LayoutDashboard className="w-4 h-4 text-primary" /> Player Dashboard
            </Link>
            <a href={retroArcadeUrl} target="_blank" rel="noreferrer" className="px-5 py-2.5 rounded-full border border-border bg-background/50 hover:bg-muted text-sm font-semibold flex items-center gap-2 transition-colors" data-testid="hero-btn-retro-arcade">
              <Gamepad2 className="w-4 h-4 text-secondary" /> Retro Arcade <span className="text-muted-foreground font-normal text-xs">(WIP)</span>
            </a>
            <Link href="/technical" className="px-5 py-2.5 rounded-full border border-border bg-background/50 hover:bg-muted text-sm font-semibold flex items-center gap-2 transition-colors" data-testid="hero-btn-technical">
              <TerminalSquare className="w-4 h-4 text-muted-foreground" /> Game Systems
            </Link>
          </div>

          <Link
            href="/fighting-game"
            className="group mt-8 inline-flex w-full sm:w-auto items-center justify-between gap-6 rounded-2xl border border-primary/30 bg-gradient-to-r from-primary/10 via-card to-secondary/10 px-6 py-4 font-bold shadow-lg shadow-primary/5 transition-all hover:-translate-y-0.5 hover:border-primary/60 hover:shadow-primary/10"
            data-testid="link-layer2-fighting-game"
          >
            <span className="flex items-center gap-3">
              <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-primary text-primary-foreground shadow-md shadow-primary/25">
                <Swords className="h-5 w-5" />
              </span>
              <span className="text-left">
                <span className="block text-xs uppercase tracking-[0.2em] text-primary">Layer 2 Sandbox</span>
                <span className="block text-base sm:text-lg">Test Layer 2 Fighting Game Sandbox</span>
              </span>
            </span>
            <ArrowRight className="h-5 w-5 shrink-0 text-primary transition-transform group-hover:translate-x-1" />
          </Link>
        </div>

        {/* Core Rules Section */}
        <div id="rules" className="mt-40 pt-20 border-t border-border/50 relative z-10 scroll-m-20">
          <div className="flex flex-col lg:flex-row lg:items-end justify-between gap-8 mb-16">
            <div className="max-w-2xl">
              <h2 className="text-4xl md:text-5xl font-display font-bold tracking-tight">TLAMA Tokenomics</h2>
              <p className="mt-6 text-xl text-muted-foreground leading-relaxed">TLAMA is engineered around clear, verifiable token rules. The ecosystem tokenomics feature an explicit, security-focused architecture designed to support a high-throughput micro-settlement network.</p>
              <p className="mt-3 text-base text-muted-foreground leading-relaxed">Every TLAMA match starts with three lives. Verified Heart Matrix upgrades can raise the server-owned maximum to five.</p>
            </div>
            <div className="flex-shrink-0 bg-card border border-border shadow-sm px-8 py-6 rounded-3xl animate-float">
              <div className="text-sm text-muted-foreground font-bold uppercase tracking-widest mb-2">Total Fixed Supply</div>
              <div className="text-3xl sm:text-4xl font-display font-bold font-mono tracking-tighter text-foreground whitespace-nowrap">1,000,000,000</div>
              <div className="text-sm font-medium text-primary mt-2 flex items-center gap-1">
                <CheckCircle2 className="w-4 h-4" /> Intended permanent hard cap
              </div>
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
            {/* Rule 1 */}
            <div className="group bg-card border border-border shadow-sm p-8 rounded-3xl hover:shadow-2xl hover:shadow-primary/5 hover:-translate-y-1 transition-all duration-300">
              <div className="w-14 h-14 rounded-2xl bg-primary/10 text-primary flex items-center justify-center mb-6 group-hover:scale-110 group-hover:bg-primary group-hover:text-primary-foreground transition-all duration-300">
                <Ban className="w-7 h-7" />
              </div>
              <h3 className="text-2xl font-display font-bold mb-4">No Future Minting</h3>
              <p className="text-muted-foreground leading-relaxed text-lg">The token blueprint enforces a hard-capped total supply of exactly 1 billion tokens, hardcoded to permanently remove mint authority immediately upon deployment so no additional supply can ever be created.</p>
            </div>

            {/* Rule 2 */}
            <div className="group bg-card border border-border shadow-sm p-8 rounded-3xl hover:shadow-2xl hover:shadow-secondary/5 hover:-translate-y-1 transition-all duration-300">
              <div className="w-14 h-14 rounded-2xl bg-secondary/10 text-secondary flex items-center justify-center mb-6 group-hover:scale-110 group-hover:bg-secondary group-hover:text-secondary-foreground transition-all duration-300">
                <Lock className="w-7 h-7" />
              </div>
              <h3 className="text-2xl font-display font-bold mb-4">No Freeze Authority</h3>
              <p className="text-muted-foreground leading-relaxed text-lg">The token architecture is strictly engineered without a freeze authority, ensuring that the final deployed token contract contains no administrative hooks capable of freezing user accounts.</p>
            </div>

            {/* Rule 3 */}
            <div className="group bg-card border border-border shadow-sm p-8 rounded-3xl hover:shadow-2xl hover:shadow-primary/5 hover:-translate-y-1 transition-all duration-300">
              <div className="w-14 h-14 rounded-2xl bg-primary/10 text-primary flex items-center justify-center mb-6 group-hover:scale-110 group-hover:bg-primary group-hover:text-primary-foreground transition-all duration-300">
                <Activity className="w-7 h-7" />
              </div>
              <h3 className="text-2xl font-display font-bold mb-4">Prepared Transfer Rules</h3>
              <p className="text-muted-foreground leading-relaxed text-lg">The Token-2022 Transfer Hook and Pool Adapter framework elements are intentionally isolated inside a secure Fail-Closed/Devnet Simulation gate. This is a controlled safety boundary, not a universal exploit patch or a claim of live on-chain execution.</p>
            </div>

            {/* Rule 4 */}
            <div className="group bg-card border border-border shadow-sm p-8 rounded-3xl hover:shadow-2xl hover:-translate-y-1 transition-all duration-300 md:col-span-2">
              <div className="w-14 h-14 rounded-2xl bg-foreground text-background flex items-center justify-center mb-6 group-hover:scale-110 transition-transform duration-300">
                <Database className="w-7 h-7" />
              </div>
              <h3 className="text-2xl font-display font-bold mb-4">Immutable Metadata</h3>
              <p className="text-muted-foreground leading-relaxed text-lg max-w-2xl">The token metadata is structurally locked as immutable within the deployment configuration, ensuring that token names, symbols, and core identity properties can never be altered post-launch.</p>
            </div>

            {/* CTA Box */}
            <div className="group relative bg-gradient-to-br from-foreground to-foreground/90 text-background p-8 rounded-3xl overflow-hidden flex flex-col justify-between hover:shadow-2xl hover:-translate-y-1 transition-all duration-300">
              <div className="absolute top-[-50%] right-[-20%] w-64 h-64 bg-primary/40 blur-[60px] rounded-full pointer-events-none" />
              <div className="relative z-10">
                <div className="w-14 h-14 rounded-2xl bg-background/10 text-background flex items-center justify-center mb-6 backdrop-blur-sm">
                  <Shield className="w-7 h-7" />
                </div>
                <h3 className="text-2xl font-display font-bold mb-4">Prepared State</h3>
                <p className="text-background/80 leading-relaxed mb-8 text-lg font-medium">This multi-layer infrastructure is fully mapped, sandbox-tested, and structurally configured for secure future network execution, maintaining total transparent visibility before launch.</p>
              </div>
              <a href="#verification" className="relative z-10 flex items-center gap-2 text-sm font-bold uppercase tracking-widest text-primary hover:text-white transition-colors group-hover:translate-x-2 duration-300" data-testid="card-link-verification">
                Read verification notes <ChevronRight className="w-4 h-4" />
              </a>
            </div>
          </div>
        </div>

        {/* Verification Section */}
        <div id="verification" className="mt-40 bg-card border border-border/50 shadow-xl rounded-[3rem] p-8 sm:p-12 md:p-20 relative overflow-hidden scroll-m-20">
          <div className="absolute top-0 right-0 w-1/2 h-full bg-gradient-to-l from-primary/5 to-transparent pointer-events-none" />

          <div className="max-w-3xl mx-auto text-center relative z-10">
            <div className="w-24 h-24 bg-background rounded-3xl shadow-lg border border-border flex items-center justify-center mx-auto mb-10 rotate-3 hover:rotate-12 transition-transform duration-500">
              <FileCode2 className="w-12 h-12 text-primary" />
            </div>
            <h2 className="text-4xl md:text-5xl lg:text-6xl font-display font-bold mb-8">Built for Verification</h2>
            <p className="text-xl text-muted-foreground mb-12 leading-relaxed">Repository verification covers 59 total test files and 601 total high-concurrency stress test cases, including 159 dedicated tests for the Webhook Rail Pro module.</p>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 text-left">
              {[
                "Open-source offline preparation",
                "Privileged authorities removed in the intended final state",
                "No deployment or trading claims",
                "Parameters ready for post-launch verification"
              ].map((item, i) => (
                <div key={i} className="flex items-center gap-4 bg-background/50 backdrop-blur-sm p-5 rounded-2xl border border-border/50 hover:bg-background transition-colors shadow-sm">
                  <div className="w-8 h-8 rounded-full bg-primary/10 flex items-center justify-center flex-shrink-0">
                    <CheckCircle2 className="w-5 h-5 text-primary" />
                  </div>
                  <span className="font-semibold text-foreground">{item}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      </main>

      {/* Footer */}
      <footer className="w-full border-t border-border/50 bg-card py-16 mt-auto relative z-10">
        <div className="max-w-7xl mx-auto px-6 flex flex-col lg:flex-row items-center justify-between gap-10">
          <div className="flex items-center gap-4 opacity-50 grayscale hover:grayscale-0 hover:opacity-100 transition-all duration-300">
            <div className="w-10 h-10 rounded-xl bg-foreground flex items-center justify-center text-background font-display font-bold text-xl">
              TL
            </div>
            <span className="font-display font-bold text-2xl tracking-tight text-foreground">TrustLlama</span>
          </div>

          <div className="flex w-full max-w-2xl flex-col items-center gap-6 lg:w-auto">
            <nav aria-label="TrustLlama community links" className="flex flex-nowrap items-center justify-center gap-2 sm:gap-3">
              {communityLinks.map(({ label, href, icon: Icon }) => (
                <a
                  key={label}
                  href={href}
                  target="_blank"
                  rel="noreferrer"
                  aria-label={`TrustLlama on ${label}`}
                  title={label}
                  className="group flex h-11 w-11 items-center justify-center rounded-full border border-border/70 bg-background/70 text-muted-foreground shadow-sm backdrop-blur-sm transition-all duration-300 hover:-translate-y-1 hover:border-primary/60 hover:text-primary hover:shadow-[0_0_22px_hsl(var(--primary)/0.28)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 focus-visible:ring-offset-card sm:h-12 sm:w-12"
                  data-testid={`footer-social-${label.toLowerCase()}`}
                >
                  <Icon className="h-5 w-5 transition-transform duration-300 group-hover:scale-110 sm:h-[1.35rem] sm:w-[1.35rem]" aria-hidden="true" />
                </a>
              ))}
            </nav>

            <div className="text-sm font-medium text-muted-foreground text-center lg:text-left bg-muted/50 px-6 py-4 rounded-2xl border border-border/50">
              <p className="text-foreground font-bold mb-1 uppercase tracking-wider text-xs flex items-center justify-center lg:justify-start gap-2">
                <AlertTriangle className="w-3 h-3 text-orange-500" /> Pre-Launch Notice
              </p>
              <p>TLAMA features are currently development candidates. Tokens are not minted, deployed, or traded.</p>
            </div>
          </div>
          
          <div className="flex items-center gap-8 text-sm font-bold uppercase tracking-widest text-muted-foreground">
            <a href="#manifesto" className="hover:text-foreground transition-colors" data-testid="footer-link-manifesto">Manifesto</a>
            <a href="#rules" className="hover:text-foreground transition-colors" data-testid="footer-link-rules">Rules</a>
            <a href="#verification" className="hover:text-foreground transition-colors" data-testid="footer-link-verification">Verify</a>
          </div>
        </div>
      </footer>
    </div>
  );
}
