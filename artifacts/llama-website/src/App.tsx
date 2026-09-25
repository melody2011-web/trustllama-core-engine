import { lazy, Suspense, type ReactNode } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ErrorBoundary } from '@/components/error-boundary';
import { Toaster } from '@/components/ui/toaster';
import { TooltipProvider } from '@/components/ui/tooltip';
import NotFound from '@/pages/not-found';
import { Landing } from '@/pages/landing';
import { Arcade } from '@/pages/arcade';
import { Memes } from '@/pages/memes';
import { Technical } from '@/pages/technical';
import { FightingGame } from '@/games/fighting-game';
import {
  Route,
  Switch,
  useLocation,
  Router as WouterRouter,
} from 'wouter';

const queryClient = new QueryClient();
const TechnicalEvidence = lazy(() =>
  import('@/pages/technical-evidence').then(({ TechnicalEvidence }) => ({
    default: TechnicalEvidence,
  })),
);
const BuyPage = lazy(() =>
  import('@/pages/buy').then(({ BuyPage }) => ({ default: BuyPage })),
);

function Router() {
  return (
    <RoutedErrorBoundary>
      <Suspense
        fallback={
          <div
            className="flex min-h-[100dvh] items-center justify-center bg-background font-display font-bold text-foreground"
            data-testid="status-page-loading"
          >
            Loading…
          </div>
        }
      >
        <Switch>
          <Route path="/" component={Landing} />
          <Route path="/arcade" component={Arcade} />
          <Route path="/fighting-game" component={FightingGame} />
          <Route path="/memes" component={Memes} />
          <Route path="/technical" component={Technical} />
          <Route path="/technical-evidence" component={TechnicalEvidence} />
          <Route path="/buy" component={BuyPage} />
          <Route component={NotFound} />
        </Switch>
      </Suspense>
    </RoutedErrorBoundary>
  );
}

function RoutedErrorBoundary({ children }: { children: ReactNode }) {
  const [location] = useLocation();
  return <ErrorBoundary resetKey={location}>{children}</ErrorBoundary>;
}

function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <WouterRouter base={import.meta.env.BASE_URL?.replace(/\/$/, '') || ''}>
          <Router />
        </WouterRouter>
        <Toaster />
      </TooltipProvider>
    </QueryClientProvider>
  );
}

export default App;
