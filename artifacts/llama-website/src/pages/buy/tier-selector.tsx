import { Server, Terminal, Building2, ChevronRight, Check } from "lucide-react";
import { TechnicalSpecs } from "./technical-specs";

interface TierSelectorProps {
  onSelect: (tier: "indie" | "startup" | "enterprise" | "webhook_rail_pro") => void;
}

const tiers = [
  {
    id: "indie" as const,
    name: "Indie Developer License",
    price: 350,
    icon: Terminal,
    description: "30Hz FastAPI/WebSocket arcade backend and local SQLite WAL persistence for independent teams.",
    features: [
      "30Hz FastAPI/WebSocket server-authoritative gameplay logic",
      "Local SQLite Write-Ahead Logging (WAL) persistence",
      "Server-owned game state and WebSocket session handling",
      "Master SHA-256 build checksum manifest included",
      "Repository-wide verification: 59 test files, 601 high-concurrency stress test cases."
    ]
  },
  {
    id: "startup" as const,
    name: "Startup Studio License",
    price: 750,
    icon: Server,
    description: "30Hz FastAPI/WebSocket arcade backend with local SQLite WAL persistence for growing studios.",
    features: [
      "30Hz FastAPI/WebSocket server-authoritative gameplay logic",
      "Local SQLite Write-Ahead Logging (WAL) persistence",
      "Server-owned game state and WebSocket session handling",
      "Advanced automated webhook delivery rails included",
      "Multi-project commercial licensing rights",
      "Repository-wide verification: 59 test files, 601 high-concurrency stress test cases."
    ],
    popular: true
  },
  {
    id: "enterprise" as const,
    name: "Enterprise Commercial License",
    price: 1950,
    icon: Building2,
    description: "30Hz FastAPI/WebSocket arcade backend and local SQLite WAL persistence for established studios.",
    features: [
      "30Hz FastAPI/WebSocket server-authoritative gameplay logic",
      "Local SQLite Write-Ahead Logging (WAL) persistence",
      "Server-owned game state and WebSocket session handling",
      "Full institutional, unrestricted high-throughput organizational usage rights",
      "Priority production-grade master file distribution framework",
      "Repository-wide verification: 59 test files, 601 high-concurrency stress test cases."
    ]
  }
];

export function TierSelector({ onSelect }: TierSelectorProps) {
  return (
    <div className="w-full animate-fade-in">
      <div className="mb-12 text-center md:text-left">
        <h1 className="text-4xl md:text-5xl font-display font-bold mb-4 tracking-tight">Select a License Tier</h1>
        <p className="text-lg text-muted-foreground max-w-2xl mx-auto md:mx-0">
          Choose the commercial package that matches your organization. Final license scope is governed by the executed licensing agreement; payment is settled in native USDC on Solana.
        </p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        {tiers.map((tier) => (
          <div 
            key={tier.id}
            className={`relative flex flex-col bg-card rounded-2xl border p-8 transition-all duration-300 hover:shadow-xl ${
              tier.popular 
                ? "border-primary shadow-lg shadow-primary/10 scale-100 md:scale-105 z-10 bg-gradient-to-b from-card to-primary/5" 
                : "border-border hover:border-primary/50"
            }`}
          >
            {tier.popular && (
              <div className="absolute top-0 left-1/2 -translate-x-1/2 -translate-y-1/2 bg-primary text-primary-foreground text-[10px] font-bold uppercase tracking-[0.2em] py-1.5 px-4 rounded-full shadow-md">
                Most Chosen
              </div>
            )}
            
            <div className="flex items-center gap-4 mb-6">
              <div className={`w-14 h-14 rounded-2xl flex items-center justify-center shrink-0 ${tier.popular ? 'bg-primary/10 text-primary' : 'bg-muted text-muted-foreground'}`}>
                <tier.icon className="w-7 h-7" />
              </div>
              <h2 className="text-xl font-display font-bold leading-tight">{tier.name}</h2>
            </div>
            
            <div className="mb-8 pb-8 border-b border-border/50">
              <div className="flex items-baseline gap-1.5">
                <span className="text-5xl font-display font-bold tracking-tighter">{tier.price}</span>
                <span className="text-muted-foreground font-bold uppercase tracking-widest text-sm">USDC</span>
              </div>
              <p className="text-sm text-muted-foreground mt-4 leading-relaxed h-10">{tier.description}</p>
            </div>
            
            <ul className="space-y-4 mb-8 flex-1">
              {tier.features.map((feature, i) => (
                <li key={i} className="flex items-start gap-3 text-sm font-medium">
                  <div className="mt-0.5 rounded-full bg-primary/10 p-0.5 text-primary shrink-0">
                    <Check className="w-3.5 h-3.5" strokeWidth={3} />
                  </div>
                  <span className="text-foreground/90 leading-tight">{feature}</span>
                </li>
              ))}
            </ul>
            
            <button
              onClick={() => onSelect(tier.id)}
              className={`w-full py-4 px-6 rounded-xl font-bold flex items-center justify-center gap-2 transition-all duration-300 group ${
                tier.popular 
                  ? "bg-primary text-primary-foreground hover:bg-primary/90 shadow-lg shadow-primary/25 hover:-translate-y-0.5" 
                  : "bg-muted text-foreground hover:bg-foreground hover:text-background"
              }`}
            >
              Continue to Checkout <ChevronRight className="w-4 h-4 transition-transform group-hover:translate-x-1" />
            </button>
          </div>
        ))}
      </div>

      <TechnicalSpecs />

      <section aria-labelledby="webhook-rail-heading" className="relative mt-12 flex w-full flex-col rounded-2xl border border-border bg-card p-8 transition-all duration-300 hover:border-primary/50 hover:shadow-xl md:w-[calc(33.333%_-_1rem)]">
        <p className="mb-6 text-xs font-bold uppercase tracking-widest text-primary">B2B software add-on · Technical tools</p>
        <div className="mb-6 flex items-center gap-4">
          <div className="flex h-14 w-14 shrink-0 items-center justify-center rounded-2xl bg-muted text-muted-foreground">
            <Server className="h-7 w-7" />
          </div>
          <h2 id="webhook-rail-heading" className="font-display text-xl font-bold leading-tight">Solana Webhook Core Rail Pro</h2>
        </div>
        <div className="mb-8 border-b border-border/50 pb-8">
          <div className="flex items-baseline gap-1.5">
            <span className="font-display text-5xl font-bold tracking-tighter">$450</span>
            <span className="text-sm font-bold uppercase tracking-widest text-muted-foreground">USDC</span>
          </div>
          <p className="mt-4 text-sm leading-relaxed text-muted-foreground">
            Standalone integration rail for authenticated Solana webhook delivery and resumable RPC event processing.
          </p>
        </div>
        <ul className="space-y-4">
          {[
            "Cryptographic HMAC-SHA256 verification",
            "Asynchronous RPC streaming with automated failover",
            "Localized SQLite persistent cursor storage",
            "159 dedicated tests for the Webhook Rail Pro module.",
          ].map((specification) => (
            <li key={specification} className="flex items-start gap-3 text-sm font-medium">
              <div className="mt-0.5 shrink-0 rounded-full bg-primary/10 p-0.5 text-primary">
                <Check className="h-3.5 w-3.5" strokeWidth={3} />
              </div>
              <span className="leading-tight text-foreground/90">{specification}</span>
            </li>
          ))}
        </ul>
        <div className="mt-auto pt-8">
          <button
            onClick={() => onSelect("webhook_rail_pro")}
            className="flex w-full items-center justify-center gap-2 rounded-xl bg-muted px-6 py-4 font-bold text-foreground transition-all duration-300 hover:bg-foreground hover:text-background"
          >
            Continue to Checkout <ChevronRight className="w-4 h-4" />
          </button>
        </div>
      </section>
    </div>
  );
}
