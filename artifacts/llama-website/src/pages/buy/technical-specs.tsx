const specifications = [
  {
    label: "Core Engine Stability",
    detail: "Repository-wide: 59 total test files and 601 total high-concurrency stress test cases.",
  },
  {
    label: "Thread Integrity",
    detail: "Verified safe multi-threaded concurrency using localized SQLite Write-Ahead Logging (WAL) state engines.",
  },
  {
    label: "Solana Webhook Delivery",
    detail: "Webhook Rail Pro has 159 dedicated tests for authenticated delivery and recovery paths.",
  },
  {
    label: "Isolated Token Framework",
    detail: "The Token-2022 Transfer Hook and Pool Adapter framework elements run inside a secure Fail-Closed/Devnet Simulation gate, not as a universal exploit patch.",
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