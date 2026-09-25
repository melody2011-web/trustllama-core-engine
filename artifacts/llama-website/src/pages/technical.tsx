import {
  ArrowLeft,
  CheckCircle2,
  Cpu,
  Gauge,
  Gamepad2,
  Server,
  ShieldCheck,
  Smartphone,
} from 'lucide-react';
import { useEffect } from 'react';
import { Link } from 'wouter';

const stages = [
  'Bounded movement and lane hazards',
  'Tighter bounds and vertical hazards',
  'Deterministic moving obstacles',
  'Orbital ghost encounters',
  'Generated maze navigation',
  'Wrap-tunnel maze traversal',
  'Server-owned flight physics',
  'Buoyancy and oxygen systems',
  'Swept-collision platforming',
  'Authoritative Citadel boss finale',
] as const;

const systems = [
  {
    icon: Server,
    title: 'Authoritative State',
    description:
      'The server owns hazards, lives, collections, stage progression, advanced physics, and final victory. Browsers render confirmed snapshots and submit bounded controls.',
  },
  {
    icon: Smartphone,
    title: 'WebKit Mobile Controls',
    description:
      'Responsive touch layouts use CSS-pixel coordinates, Pointer Events, and explicit iOS touch fallbacks. Keyboard and touch controls share the same validation boundary.',
  },
  {
    icon: Cpu,
    title: 'Deterministic Systems',
    description:
      'Generated stages, moving hazards, timers, and simulation steps use bounded inputs and server-owned state so each accepted action produces a controlled result.',
  },
  {
    icon: ShieldCheck,
    title: 'Narrow Input Contracts',
    description:
      'Ordinary stages finite-check and clamp position requests. Advanced stages accept sequenced intent while the server calculates movement, collisions, and outcomes.',
  },
] as const;

export function Technical() {
  useEffect(() => {
    const previousTitle = document.title;
    const description = document.querySelector<HTMLMetaElement>(
      'meta[name="description"]',
    );
    const previousDescription = description?.content;

    document.title = 'Technical Documentation | TLAMA';
    if (description) {
      description.content =
        'Technical summary of the TLAMA ten-stage server-authoritative engine, deterministic gameplay systems, and WebKit mobile controls.';
    }

    return () => {
      document.title = previousTitle;
      if (description && previousDescription) {
        description.content = previousDescription;
      }
    };
  }, []);

  return (
    <div className="game-platform-theme min-h-[100dvh] bg-background text-foreground">
      <header className="sticky top-0 z-50 border-b border-primary/20 bg-background/90 backdrop-blur-xl">
        <div className="mx-auto flex h-20 max-w-7xl items-center justify-between px-6">
          <Link
            href="/"
            className="inline-flex items-center gap-2 font-arcade text-sm font-bold uppercase tracking-widest text-muted-foreground transition-colors hover:text-primary"
          >
            <ArrowLeft className="h-4 w-4" />
            Back to TLAMA
          </Link>
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl border border-primary/50 bg-primary/10 font-arcade text-xl font-bold text-primary shadow-[0_0_15px_rgba(0,255,255,0.3)]">
              TL
            </div>
            <span className="hidden font-arcade text-xl font-bold tracking-widest text-primary neon-text sm:inline">
              SYSTEMS
            </span>
          </div>
        </div>
      </header>

      <main>
        <section className="relative overflow-hidden border-b border-primary/20 px-6 py-20 md:py-28">
          <div className="absolute inset-0 bg-grid opacity-30" />
          <div className="relative mx-auto max-w-7xl">
            <div className="mb-6 inline-flex items-center gap-2 border border-primary/30 bg-primary/10 px-3 py-1 font-arcade text-xs font-bold uppercase tracking-[0.2em] text-primary">
              <Gauge className="h-4 w-4" />
              Architecture Reference
            </div>
            <h1 className="max-w-5xl font-arcade text-4xl font-bold uppercase leading-tight tracking-tight sm:text-6xl md:text-7xl">
              Ten-stage
              <span className="block text-primary neon-text">
                authoritative engine
              </span>
            </h1>
            <p className="mt-8 max-w-3xl text-lg leading-relaxed text-muted-foreground md:text-xl">
              TLAMA separates visual game clients from the systems that
              decide gameplay outcomes. The browser handles presentation and
              controls; the server validates state transitions and publishes
              confirmed snapshots.
            </p>
          </div>
        </section>

        <section className="px-6 py-20 md:py-24" aria-labelledby="systems-heading">
          <div className="mx-auto max-w-7xl">
            <div className="max-w-3xl">
              <div className="mb-4 font-arcade text-sm font-bold uppercase tracking-[0.2em] text-secondary">
                Core systems
              </div>
              <h2
                id="systems-heading"
                className="font-arcade text-3xl font-bold uppercase sm:text-5xl"
              >
                One source of gameplay truth
              </h2>
            </div>

            <div className="mt-12 grid gap-6 md:grid-cols-2">
              {systems.map(({ icon: Icon, title, description }) => (
                <article
                  key={title}
                  className="border border-primary/20 bg-card/70 p-7 transition-colors hover:border-primary/50"
                >
                  <div className="mb-5 flex h-12 w-12 items-center justify-center border border-primary/30 bg-primary/10 text-primary">
                    <Icon className="h-6 w-6" />
                  </div>
                  <h3 className="font-arcade text-xl font-bold uppercase tracking-wide">
                    {title}
                  </h3>
                  <p className="mt-4 leading-relaxed text-muted-foreground">
                    {description}
                  </p>
                </article>
              ))}
            </div>
          </div>
        </section>

        <section className="border-y border-secondary/20 bg-card/30 px-6 py-20 md:py-24">
          <div className="mx-auto max-w-7xl">
            <div className="grid gap-12 lg:grid-cols-[0.8fr_1.2fr]">
              <div>
                <div className="mb-4 inline-flex items-center gap-2 font-arcade text-sm font-bold uppercase tracking-[0.2em] text-secondary">
                  <Gamepad2 className="h-4 w-4" />
                  Stage progression
                </div>
                <h2 className="font-arcade text-3xl font-bold uppercase sm:text-5xl">
                  Ten distinct gameplay stages
                </h2>
                <p className="mt-6 text-lg leading-relaxed text-muted-foreground">
                  Each stage narrows the client contract to the controls its
                  mechanics require. Later stages move complete physics and
                  collision resolution into server-owned simulations.
                </p>
              </div>

              <ol className="grid gap-3 sm:grid-cols-2">
                {stages.map((stage, index) => (
                  <li
                    key={stage}
                    className="flex items-center gap-4 border border-border bg-background/70 p-4"
                  >
                    <span className="font-arcade text-sm font-bold text-secondary">
                      {String(index + 1).padStart(2, '0')}
                    </span>
                    <span className="text-sm font-semibold">{stage}</span>
                  </li>
                ))}
              </ol>
            </div>
          </div>
        </section>

        <section className="px-6 py-20 md:py-24">
          <div className="mx-auto max-w-7xl border border-primary/30 bg-primary/5 p-8 md:p-12">
            <div className="grid gap-10 lg:grid-cols-[1fr_auto] lg:items-center">
              <div>
                <h2 className="font-arcade text-3xl font-bold uppercase">
                  Confirmed outcomes
                </h2>
                <div className="mt-6 grid gap-4 text-muted-foreground sm:grid-cols-2">
                  {[
                    'Server-confirmed collision and collection',
                    'Exact shield and recovery deadlines',
                    'Bounded simulation delta time',
                    'Boss defeat required for final victory',
                  ].map((item) => (
                    <div key={item} className="flex items-start gap-3">
                      <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0 text-primary" />
                      <span>{item}</span>
                    </div>
                  ))}
                </div>
              </div>
              <Link
                href="/memes"
                className="inline-flex min-h-14 items-center justify-center border border-secondary bg-secondary/10 px-7 font-arcade text-sm font-bold uppercase tracking-widest text-secondary transition-colors hover:bg-secondary hover:text-secondary-foreground"
              >
                View Artwork Gallery
              </Link>
            </div>
          </div>
        </section>
      </main>
    </div>
  );
}