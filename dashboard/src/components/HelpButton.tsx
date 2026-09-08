import { useState } from 'react';

/** A small "?" toggle next to a view's heading that reveals a plain-language explanation of what
 * the view shows and how to read it. Every view has one of these — reducing the amount a first-time
 * reader has to already know (plan.md's chart-design principles are correct but assume familiarity
 * with the benchmark; this is the on-ramp for everyone else). */
export function HelpButton({ children }: { children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <span className="relative inline-block align-middle">
      <button
        type="button"
        aria-label="What does this view show?"
        onClick={() => setOpen((o) => !o)}
        className="ml-2 h-5 w-5 rounded-full border border-[var(--color-border)] text-xs leading-none text-[var(--color-text-muted)] hover:bg-[var(--color-accent-muted)]"
      >
        ?
      </button>
      {open && (
        <div className="absolute left-0 top-6 z-10 w-80 rounded border border-[var(--color-border)] bg-[var(--color-surface)] p-3 text-xs leading-relaxed text-[var(--color-text)] shadow-lg">
          {children}
          <button
            type="button"
            onClick={() => setOpen(false)}
            className="mt-2 block text-[var(--color-text-muted)] underline"
          >
            close
          </button>
        </div>
      )}
    </span>
  );
}
