import { AlertCircle } from "lucide-react";
import { Link } from "wouter";

export default function NotFound() {
  return (
    <div className="min-h-screen w-full flex items-center justify-center bg-background">
      <div className="text-center p-8 rounded-3xl border border-border bg-card shadow-sm max-w-md w-full mx-4 animate-fade-up">
        <AlertCircle className="w-12 h-12 text-destructive mx-auto mb-6" />
        <h1 className="text-3xl font-display font-bold text-foreground mb-3">404</h1>
        <p className="text-muted-foreground mb-8 text-lg">The page you're looking for doesn't exist.</p>
        <Link href="/" className="inline-flex items-center justify-center px-6 py-3 bg-foreground text-background font-bold rounded-full hover:bg-foreground/90 transition-colors">
          Return to Hub
        </Link>
      </div>
    </div>
  );
}
