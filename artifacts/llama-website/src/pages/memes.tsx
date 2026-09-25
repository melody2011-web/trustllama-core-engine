import { ArrowLeft, Download, Images } from 'lucide-react';
import { Link } from 'wouter';

const memes: ReadonlyArray<{ title: string; file: string; width: number; height: number; wide?: boolean }> = [
  { title: 'Trust Llama', file: '2026-09-06-tlama-trust-llama-v01.png', width: 1080, height: 1080 },
  { title: 'Community Llama', file: '2026-08-30-tlama-community-v01.png', width: 1080, height: 1080 },
  { title: 'Llama Sheriff', file: '2026-08-30-tlama-sheriff-v01.png', width: 1080, height: 1080 },
  { title: 'Space Llama', file: '2026-08-30-tlama-space-v01.png', width: 1080, height: 1080 },
  { title: 'Vault Llama', file: '2026-08-30-tlama-vault-v01.png', width: 1080, height: 1080 },
  { title: 'Verify, Don’t Trust', file: '2026-08-30-tlama-verify-v01.png', width: 1080, height: 1080 },
  { title: 'Arcade Shield', file: '2026-09-11-tlama-arcade-shield-v01.png', width: 1080, height: 1080 },
  { title: 'Built in Public', file: '2026-09-11-tlama-built-in-public-v01.png', width: 1080, height: 1080 },
  { title: 'Evidence Trail', file: '2026-09-11-tlama-evidence-trail-v01.png', width: 1080, height: 1080 },
  { title: 'Same Rules, Every Wallet', file: '2026-09-11-tlama-same-rules-v01.png', width: 1080, height: 1080 },
  { title: 'Trust the Proof', file: '2026-09-11-tlama-trust-the-proof-v01.png', width: 1080, height: 1080 },
];

export function Memes() {
  const baseUrl = import.meta.env.BASE_URL;

  return (
    <div className="game-platform-theme min-h-[100dvh] bg-background text-foreground bg-grid">
      <header className="sticky top-0 z-50 border-b border-primary/20 bg-background/90 backdrop-blur-xl">
        <div className="mx-auto flex h-20 max-w-7xl items-center justify-between px-6">
          <Link href="/" className="inline-flex items-center gap-2 text-sm font-bold text-muted-foreground transition-colors hover:text-primary">
            <ArrowLeft className="h-4 w-4" />
            Back to Arcade
          </Link>
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl border border-primary/50 bg-primary/10 font-arcade text-xl font-bold text-primary shadow-[0_0_15px_rgba(0,255,255,0.3)]">TL</div>
            <span className="hidden font-arcade text-xl font-bold tracking-widest sm:inline text-primary neon-text">TRUSTLLAMA</span>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-7xl px-6 py-16 md:py-24">
        <div className="max-w-3xl">
          <div className="mb-5 inline-flex items-center gap-2 text-sm font-bold uppercase tracking-[0.2em] text-secondary">
            <Images className="h-5 w-5" />
            Pixel Art Archive
          </div>
          <h1 className="font-arcade text-5xl font-extrabold tracking-tight md:text-7xl uppercase text-foreground">Official Artwork</h1>
          <p className="mt-6 text-xl leading-relaxed text-muted-foreground font-medium">
            Browse all eleven official TrustLlama memes and pixel-art assets. Open any image at full size or download it for sharing.
          </p>
        </div>

        <div className="mt-14 grid gap-8 md:grid-cols-2">
          {memes.map(({ title, file, width, height, wide }, index) => {
            const imageUrl = `${baseUrl}memes/${file}`;
            return (
              <article 
                key={file} 
                className={`overflow-hidden rounded-xl border border-border bg-card/80 backdrop-blur-sm shadow-lg transition-all hover:border-primary/50 hover:shadow-[0_0_20px_rgba(0,255,255,0.15)] group ${wide ? 'md:col-span-2' : ''} ${!wide && index === memes.length - 1 && memes.length % 2 === 0 ? 'md:col-span-2 md:mx-auto md:w-[calc(50%-1rem)]' : ''}`}
                data-testid={`article-meme-${index}`}
              >
                <a href={imageUrl} target="_blank" rel="noreferrer" className="block overflow-hidden bg-black relative" data-testid={`link-image-${index}`}>
                  <div className="absolute inset-0 bg-primary/10 opacity-0 group-hover:opacity-100 transition-opacity z-10 mix-blend-overlay pointer-events-none" />
                  <img 
                    src={imageUrl} 
                    alt={title} 
                    className={`w-full object-cover transition-transform duration-500 group-hover:scale-105 ${wide ? 'aspect-[1200/630]' : 'aspect-square'}`} 
                    loading={index === 0 ? 'eager' : 'lazy'}
                    width={width}
                    height={height}
                    data-testid={`img-meme-${index}`}
                  />
                </a>
                <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 p-5">
                  <h2 className="font-arcade text-lg sm:text-xl font-bold tracking-wide uppercase text-foreground">{title}</h2>
                  <a 
                    href={imageUrl} 
                    download={file} 
                    className="inline-flex shrink-0 items-center justify-center gap-2 rounded-md bg-secondary/10 border border-secondary/50 px-4 py-2 text-sm font-bold text-secondary transition-all hover:bg-secondary hover:text-secondary-foreground shadow-[0_0_10px_rgba(255,0,255,0.2)]"
                    data-testid={`btn-download-${index}`}
                  >
                    <Download className="h-4 w-4" />
                    Download
                  </a>
                </div>
              </article>
            );
          })}
        </div>
      </main>
    </div>
  );
}
