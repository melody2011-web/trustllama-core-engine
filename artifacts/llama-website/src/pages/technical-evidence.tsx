import { useEffect } from 'react';
import { Link } from 'wouter';
import { ArrowLeft, FileCode2, ShieldCheck } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rawEvidenceMd from '../../../../docs/TRUSTLLAMA_TECHNICAL_EVIDENCE.md?raw';

export function TechnicalEvidence() {
  useEffect(() => {
    const previousTitle = document.title;
    const description = document.querySelector<HTMLMetaElement>(
      'meta[name="description"]',
    );
    const previousDescription = description?.content;

    document.title = 'Technical Evidence | TrustLlama';
    if (description) {
      description.content =
        'Read the full source-grounded TrustLlama technical evidence report, including architecture, security boundaries, test evidence, and Token-2022 design.';
    }

    return () => {
      document.title = previousTitle;
      if (description && previousDescription) {
        description.content = previousDescription;
      }
    };
  }, []);

  return (
    <div className="min-h-[100dvh] bg-background flex flex-col relative overflow-hidden text-foreground">
      {/* Background Decor */}
      <div className="fixed top-[-20%] left-[-10%] w-[70vw] h-[70vw] rounded-full bg-primary/5 blur-[120px] pointer-events-none" />
      <div className="fixed bottom-[-20%] right-[-10%] w-[60vw] h-[60vw] rounded-full bg-secondary/5 blur-[120px] pointer-events-none" />

      {/* Navigation */}
      <nav className="w-full border-b border-border/50 bg-background/80 backdrop-blur-xl sticky top-0 z-50">
        <div className="max-w-5xl mx-auto px-6 h-16 flex items-center justify-between">
          <div className="flex items-center gap-4">
            <Link 
              href="/" 
              className="flex items-center justify-center w-8 h-8 rounded-full hover:bg-muted transition-colors text-muted-foreground hover:text-foreground"
              data-testid="link-back-home"
            >
              <ArrowLeft className="w-4 h-4" />
            </Link>
            <div className="w-8 h-8 rounded-lg bg-foreground flex items-center justify-center text-background font-display font-bold text-sm shadow-sm">
              TL
            </div>
            <span className="font-display font-bold tracking-tight text-foreground hidden sm:inline-block">TrustLlama</span>
          </div>
          
          <div className="flex items-center gap-2 px-3 py-1.5 rounded-full bg-primary/10 text-primary border border-primary/20 shadow-sm">
            <ShieldCheck className="w-3.5 h-3.5" />
            <span className="text-[10px] font-bold uppercase tracking-widest">Evidence Report</span>
          </div>
        </div>
      </nav>

      <main className="flex-1 w-full max-w-5xl mx-auto px-6 py-12 md:py-20 relative z-10">
        <div className="mb-12 animate-fade-up opacity-0" style={{ animationDelay: '100ms' }}>
          <div className="inline-flex items-center gap-2 mb-6 text-sm font-bold uppercase tracking-widest text-muted-foreground">
            <FileCode2 className="w-4 h-4" /> Technical Documentation
          </div>
          <h1 className="text-4xl md:text-6xl font-display font-extrabold tracking-tight mb-6" data-testid="text-page-title">
            Source-Grounded Evidence
          </h1>
          <p className="text-xl text-muted-foreground leading-relaxed max-w-3xl" data-testid="text-page-description">
            This document serves as an independently verifiable report detailing the architectural boundaries, security checks, and executed state of the TrustLlama repository. 
          </p>
        </div>

        <div 
          className="bg-card/50 border border-border/50 shadow-xl shadow-black/5 rounded-[2rem] p-6 sm:p-10 md:p-16 backdrop-blur-sm animate-fade-up opacity-0" 
          style={{ animationDelay: '200ms' }}
        >
          <div className="prose prose-zinc dark:prose-invert max-w-none 
            prose-headings:font-display prose-headings:tracking-tight prose-headings:font-bold
            prose-h1:text-3xl prose-h2:text-2xl prose-h3:text-xl
            prose-h2:mt-12 prose-h2:border-b prose-h2:border-border/50 prose-h2:pb-4
            prose-a:text-primary prose-a:no-underline hover:prose-a:underline
            prose-strong:text-foreground prose-strong:font-bold
            prose-code:text-secondary prose-code:bg-secondary/10 prose-code:px-1.5 prose-code:py-0.5 prose-code:rounded-md prose-code:before:content-none prose-code:after:content-none
            prose-pre:bg-muted/50 prose-pre:border prose-pre:border-border/50 prose-pre:text-foreground
            prose-table:border-collapse prose-table:w-full prose-td:border prose-td:border-border/50 prose-td:p-4 prose-th:border prose-th:border-border/50 prose-th:p-4 prose-th:bg-muted/30 prose-th:text-left
            prose-tr:transition-colors hover:prose-tr:bg-muted/20"
            data-testid="markdown-container"
          >
            <ReactMarkdown 
              remarkPlugins={[remarkGfm]}
              components={{
                table: ({node, ...props}) => (
                  <div className="overflow-x-auto my-8 rounded-xl border border-border/50 shadow-sm bg-background/50">
                    <table className="w-full text-sm m-0" {...props} />
                  </div>
                ),
              }}
            >
              {rawEvidenceMd}
            </ReactMarkdown>
          </div>
        </div>
      </main>

      <footer className="w-full border-t border-border/50 bg-card py-12 mt-auto relative z-10">
        <div className="max-w-5xl mx-auto px-6 flex flex-col sm:flex-row items-center justify-between gap-6">
          <div className="flex items-center gap-3 opacity-70">
            <ShieldCheck className="w-5 h-5" />
            <span className="text-sm font-medium tracking-wide">Trust in Simplicity. Verify in Seconds.</span>
          </div>
          <div className="text-xs font-medium text-muted-foreground uppercase tracking-widest">
            {new Date().getUTCFullYear()} TrustLlama Protocol
          </div>
        </div>
      </footer>
    </div>
  );
}