/** Marks values that were inferred by the agent, not confirmed by humans. */
export function InferredLabel({ children = "Inferred" }: { children?: string }) {
  return (
    <span className="ml-2 inline-flex items-center rounded bg-sky-500/10 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wider text-sky-300 ring-1 ring-sky-500/25">
      {children}
    </span>
  );
}
