import { useEffect, useRef, useState } from "react";
import { formatTime } from "../utils/format";

/** Show a screenshot, and swap to the next one without going blank in between.
 *
 * Assigning a new `src` to a live `<img>` makes the browser drop the old bitmap
 * immediately and render nothing until the new file has been fetched and
 * decoded. During a run the live view receives a new screenshot every couple of
 * seconds, so that blank gap reads as the page blinking continuously.
 *
 * So the incoming image is loaded off-screen first and only becomes the
 * displayed one once it is ready. The visible `<img>` is never pointed at
 * something unloaded, so there is nothing to flash.
 */
export function ScreenshotViewer({
  src,
  url,
  timestamp,
  alt = "Browser screenshot",
}: {
  src: string | null;
  url?: string | null;
  timestamp?: string | null;
  alt?: string;
}) {
  const [fullscreen, setFullscreen] = useState(false);
  const [shown, setShown] = useState<string | null>(src);
  // Only a failure of the CURRENT src may show the empty state. This used to be
  // a plain `useState(false)` set by `onError` and never reset, so one missing
  // file left "Screenshot unavailable" on screen for the rest of the run even as
  // later screenshots arrived perfectly well.
  const [failedSrc, setFailedSrc] = useState<string | null>(null);
  const pending = useRef<HTMLImageElement | null>(null);

  useEffect(() => {
    if (!src || src === shown) return;
    // A data: URL is already in memory — swapping cannot flash, and preloading
    // it would only duplicate it.
    if (src.startsWith("data:")) {
      setShown(src);
      return;
    }
    const loader = new Image();
    pending.current = loader;
    loader.onload = () => {
      if (pending.current === loader) setShown(src);
    };
    loader.onerror = () => {
      // Keep the previous screenshot on screen and record which src failed, so
      // the empty state appears only when there is nothing good to show.
      if (pending.current === loader) setFailedSrc(src);
    };
    loader.src = src;
    return () => {
      if (pending.current === loader) pending.current = null;
      loader.onload = null;
      loader.onerror = null;
    };
  }, [src, shown]);

  // `shown` is the single source of truth for what is on screen. It starts at
  // `src` so the first render draws immediately — there is no previous image to
  // preserve yet, so nothing can flash.
  const displaySrc = shown;
  const failed = !!src && src === failedSrc && !displaySrc;

  const body = !displaySrc || failed ? (
    <div className="flex aspect-video items-center justify-center rounded-xl border border-dashed border-white/15 bg-ink-950/50 text-sm text-slate-500">
      Screenshot unavailable
    </div>
  ) : (
    <button
      type="button"
      className="group relative block w-full overflow-hidden rounded-xl border border-white/10 bg-ink-950"
      onClick={() => setFullscreen(true)}
    >
      <img
        src={displaySrc}
        alt={alt}
        className="aspect-video w-full object-contain object-top"
      />
      <span className="absolute bottom-2 right-2 rounded bg-black/60 px-2 py-1 text-[10px] uppercase tracking-wider text-slate-200 opacity-0 transition group-hover:opacity-100">
        Full screen
      </span>
    </button>
  );

  return (
    <div className="space-y-2">
      {body}
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-slate-500">
        <span className="break-all font-mono">{url || "—"}</span>
        <span>{formatTime(timestamp)}</span>
      </div>
      {fullscreen && displaySrc && !failed ? (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/85 p-4"
          onClick={() => setFullscreen(false)}
          role="dialog"
          aria-modal="true"
        >
          <img
            src={displaySrc}
            alt={alt}
            className="max-h-full max-w-full object-contain"
            onClick={(e) => e.stopPropagation()}
          />
          <button
            type="button"
            className="absolute right-4 top-4 btn-ghost"
            onClick={() => setFullscreen(false)}
          >
            Close
          </button>
        </div>
      ) : null}
    </div>
  );
}
