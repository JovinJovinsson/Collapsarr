import type { ReactNode } from "react";

interface PlaceholderViewProps {
  title: string;
  /** One-line description of what this view will do. */
  summary: string;
  /** Empty-state guidance shown inside the panel. */
  emptyState: string;
  /** Jira ticket that delivers the real view, e.g. "COL-31". */
  ticket: string;
  icon: ReactNode;
}

/**
 * Shared scaffold for the not-yet-built views. Each view (Wanted / Activity /
 * Settings) renders its own copy through this so the shell stays consistent
 * until the real screens land in COL-31/32/33.
 */
export function PlaceholderView({
  title,
  summary,
  emptyState,
  ticket,
  icon,
}: PlaceholderViewProps) {
  return (
    <section className="view">
      <header className="view__header">
        <h1 className="view__title">{title}</h1>
        <p className="view__summary">{summary}</p>
      </header>

      <div className="panel panel--empty">
        <span className="panel__icon" aria-hidden>
          {icon}
        </span>
        <p className="panel__message">{emptyState}</p>
        <span className="panel__tag">Coming in {ticket}</span>
      </div>
    </section>
  );
}
