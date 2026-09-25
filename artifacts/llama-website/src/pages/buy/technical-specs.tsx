const specifications = [
  {
    label: "Core Engine Stability",
    detail: "169 comprehensive high-concurrency stress tests executed and passed, with a reported 0% failure rate under peak network load.",
  },
  {
    label: "Thread Integrity",
    detail: "Verified safe multi-threaded concurrency using localized SQLite Write-Ahead Logging (WAL) state engines.",
  },
  {
    label: "Solana Webhook Delivery",
    detail: "Layer 3 precision transaction utilities optimized for sub-second delivery grants.",
  },
];

export function TechnicalSpecs() {
  return (
    <section aria-labelledby="technical-specs-heading" className="mt-12 border-t border-border/60 pt-10">
      <div className="mb-6">
        <p className="mb-2 text-xs font-bold uppercase tracking-widest text-primary">Reported verification benchmarks</p>
        <h2 id="technical-specs-heading" className="font-display text-2xl font-bold tracking-tight md:text-3xl">
          Technical Architecture &amp; Verification Specs
        </h2>
      </div>
      <dl className="divide-y divide-border/60 rounded-2xl border border-border bg-card px-6 shadow-sm sm:px-8">
        {specifications.map(({ label, detail }) => (
          <div key={label} className="grid gap-2 py-6 md:grid-cols-[14rem_minmax(0,1fr)] md:gap-8">
            <dt className="font-display font-bold text-foreground">{label}</dt>
            <dd className="text-sm leading-relaxed text-muted-foreground sm:text-base">{detail}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}