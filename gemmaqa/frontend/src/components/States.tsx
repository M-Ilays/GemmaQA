export function LoadingState({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="surface flex items-center gap-3 p-6 text-slate-400">
      <span className="inline-block h-2.5 w-2.5 animate-pulseSoft rounded-full bg-tide-400" />
      {label}
    </div>
  );
}

export function EmptyState({
  title,
  detail,
}: {
  title: string;
  detail?: string;
}) {
  return (
    <div className="surface p-8 text-center">
      <p className="font-display text-lg text-white">{title}</p>
      {detail ? <p className="mt-2 text-sm text-slate-400">{detail}</p> : null}
    </div>
  );
}

export function FailureState({
  title = "Something went wrong",
  detail,
  onRetry,
}: {
  title?: string;
  detail?: string;
  onRetry?: () => void;
}) {
  return (
    <div className="surface border-rose-500/20 p-6">
      <p className="text-rose-300">{title}</p>
      {detail ? <p className="mt-2 text-sm text-slate-400">{detail}</p> : null}
      {onRetry ? (
        <button type="button" className="btn-ghost mt-4" onClick={onRetry}>
          Retry
        </button>
      ) : null}
    </div>
  );
}

export function ReconnectingBanner() {
  return (
    <div className="rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-2 text-sm text-amber-100">
      WebSocket reconnecting… live updates may pause briefly.
    </div>
  );
}
