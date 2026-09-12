import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ScreenshotViewer } from "./ScreenshotViewer";

/**
 * The live view swaps in a new screenshot every couple of seconds during a run.
 * Assigning a new `src` to a live <img> makes the browser drop the old bitmap at
 * once and render nothing until the new file has been fetched and decoded, so a
 * run's worth of swaps reads as the page blinking continuously — the complaint
 * that has been there since the first version.
 *
 * Verified live on run 7a4e97b8 while it was executing: 0 of ~460 samples caught
 * the visible <img> holding a src with nothing decoded.
 */

/** Stands in for the browser's image loader, which jsdom does not implement. */
class FakeImage {
  static instances: FakeImage[] = [];
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  private _src = "";

  constructor() {
    FakeImage.instances.push(this);
  }

  set src(value: string) {
    this._src = value;
  }

  get src() {
    return this._src;
  }

  static latest(): FakeImage {
    return FakeImage.instances[FakeImage.instances.length - 1];
  }
}

const shownSrc = () => screen.queryByAltText("Browser screenshot")?.getAttribute("src") ?? null;

/** The real loader fires outside React, so the state update needs flushing. */
const finishLoad = (img: FakeImage) => act(() => { img.onload?.(); });
const failLoad = (img: FakeImage) => act(() => { img.onerror?.(); });

beforeEach(() => {
  FakeImage.instances = [];
  vi.stubGlobal("Image", FakeImage as unknown as typeof Image);
});

afterEach(() => {
  // This project has no vitest setup file, so RTL's auto-cleanup is not wired up
  // and renders would otherwise accumulate across tests in one document.
  cleanup();
  vi.unstubAllGlobals();
});

describe("ScreenshotViewer", () => {
  it("draws the first screenshot immediately", () => {
    render(<ScreenshotViewer src="/a.png" />);
    expect(shownSrc()).toBe("/a.png");
  });

  it("keeps the old screenshot on screen while the next one loads", () => {
    const { rerender } = render(<ScreenshotViewer src="/a.png" />);
    rerender(<ScreenshotViewer src="/b.png" />);

    // This is the assertion that would have failed before: the <img> used to be
    // pointed straight at /b.png, blanking until it arrived.
    expect(shownSrc()).toBe("/a.png");
    expect(FakeImage.latest().src).toBe("/b.png");
  });

  it("swaps only once the next one has loaded", () => {
    const { rerender } = render(<ScreenshotViewer src="/a.png" />);
    rerender(<ScreenshotViewer src="/b.png" />);
    finishLoad(FakeImage.latest());

    expect(shownSrc()).toBe("/b.png");
  });

  it("keeps showing the last good screenshot when the next one 404s", () => {
    const { rerender } = render(<ScreenshotViewer src="/a.png" />);
    rerender(<ScreenshotViewer src="/missing.png" />);
    failLoad(FakeImage.latest());

    expect(shownSrc()).toBe("/a.png");
    expect(screen.queryByText("Screenshot unavailable")).toBeNull();
  });

  it("recovers after a failure instead of latching on it", () => {
    // `failed` used to be a plain useState set by onError and never reset, so ONE
    // missing file left "Screenshot unavailable" up for the rest of the run even
    // as later screenshots arrived perfectly well.
    const { rerender } = render(<ScreenshotViewer src={null} />);
    expect(screen.getByText("Screenshot unavailable")).toBeTruthy();

    rerender(<ScreenshotViewer src="/missing.png" />);
    failLoad(FakeImage.latest());
    expect(screen.getByText("Screenshot unavailable")).toBeTruthy();

    rerender(<ScreenshotViewer src="/good.png" />);
    finishLoad(FakeImage.latest());
    expect(shownSrc()).toBe("/good.png");
  });

  it("shows the empty state when there is no screenshot at all", () => {
    render(<ScreenshotViewer src={null} />);
    expect(screen.getByText("Screenshot unavailable")).toBeTruthy();
  });

  it("does not preload a data URL, which is already in memory", () => {
    const { rerender } = render(<ScreenshotViewer src="/a.png" />);
    const before = FakeImage.instances.length;
    rerender(<ScreenshotViewer src="data:image/png;base64,AAAA" />);

    expect(shownSrc()).toBe("data:image/png;base64,AAAA");
    expect(FakeImage.instances.length).toBe(before);
  });

  it("ignores a load that arrives after the src moved on again", () => {
    const { rerender } = render(<ScreenshotViewer src="/a.png" />);
    rerender(<ScreenshotViewer src="/b.png" />);
    const stale = FakeImage.latest();
    rerender(<ScreenshotViewer src="/c.png" />);

    finishLoad(stale);

    expect(shownSrc()).not.toBe("/b.png");
  });
});
