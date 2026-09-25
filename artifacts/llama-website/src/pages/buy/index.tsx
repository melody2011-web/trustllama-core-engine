import { useState } from "react";
import { Link } from "wouter";
import { ArrowLeft, Shield, ShieldCheck } from "lucide-react";
import { TierSelector } from "./tier-selector";
import { CheckoutFlow } from "./checkout-flow";

export function BuyPage() {
  const [selectedTier, setSelectedTier] = useState<"indie" | "startup" | "enterprise" | "webhook_rail_pro" | null>(null);
  
  return (
    <div className="min-h-[100dvh] bg-background text-foreground flex flex-col font-sans selection:bg-primary/20 selection:text-primary">
      <nav className="w-full border-b border-border/50 bg-background/80 backdrop-blur-xl sticky top-0 z-50">
        <div className="max-w-7xl mx-auto px-6 h-20 flex items-center justify-between">
          <div className="flex items-center gap-2 sm:gap-4">
            <Link href="/" className="p-2 -ml-2 rounded-full hover:bg-muted transition-colors text-muted-foreground hover:text-foreground">
              <ArrowLeft className="w-5 h-5" />
            </Link>
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 rounded-xl bg-foreground flex items-center justify-center text-background font-display font-bold text-xl shadow-lg">
                TL
              </div>
              <span className="font-display font-bold text-2xl tracking-tight text-foreground hidden sm:inline-block">TrustLlama</span>
            </div>
            <div className="h-6 w-[1px] bg-border mx-2 hidden sm:block" />
            <span className="font-display font-semibold tracking-wide text-primary hidden xs:inline-block">Commercial Licensing</span>
          </div>
          <div className="flex items-center gap-2 text-sm font-medium text-muted-foreground bg-muted/50 px-3 py-1.5 rounded-full border border-border/50">
            <ShieldCheck className="w-4 h-4 text-primary" />
            <span className="hidden sm:inline">Enterprise Grade</span>
          </div>
        </div>
      </nav>

      <main className="flex-1 w-full max-w-6xl mx-auto px-6 py-12 md:py-20 flex flex-col">
        {!selectedTier ? (
          <TierSelector onSelect={setSelectedTier} />
        ) : (
          <CheckoutFlow tier={selectedTier} onCancel={() => setSelectedTier(null)} />
        )}
      </main>
      
      <footer className="w-full border-t border-border/50 bg-card py-8 mt-auto">
        <div className="max-w-6xl mx-auto px-6 flex flex-col md:flex-row items-center justify-between gap-4 text-sm font-medium text-muted-foreground">
          <div className="flex items-center gap-2">
            <Shield className="w-4 h-4 text-primary" />
            <span>Secure Solana Native USDC Settlement</span>
          </div>
          <div className="text-xs tracking-wider uppercase opacity-70">
            © {new Date().getFullYear()} TrustLlama Infrastructure.
          </div>
        </div>
      </footer>
    </div>
  );
}
