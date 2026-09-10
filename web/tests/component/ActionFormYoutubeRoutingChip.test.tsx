// @vitest-environment jsdom
//
// fetch_url remains a plain byte downloader with VISIBLE routing chips and no
// silent auto-sorting to other actions: when the
// selected/available link column's sampled values look like supported media URLs on
// the fetch_url form, a chip suggests the
// Download media action with the same column preselected — a VISIBLE
// suggestion only. It never blocks Run and never auto-redirects; the sample
// itself is a best-effort client-side read (ActionPanel.tsx's rowCache-backed
// sampleColumnValues), so an absent sample (prop omitted, or nothing loaded
// yet) simply means no chip, never an error.

import "@testing-library/jest-dom/vitest";
import { cleanup, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { columnDef } from "../support/domainFixtures";
import {
  sheetMeta,
} from "../support/actionFormFixtures";
import {
  mockActionApiDefaults,
} from "../support/renderActionForm";
import { renderMediaAction } from "../support/renderMediaAction";
import { looksLikeSupportedMediaUrl } from "../../src/components/action-panel/formControlHelpers";

afterEach(cleanup);

beforeEach(() => {
  mockActionApiDefaults();
});

const YOUTUBE_SAMPLE = [
  "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
  "https://youtu.be/dQw4w9WgXcQ",
  "https://www.youtube.com/watch?v=abcdefghijk",
];

const GENERIC_SAMPLE = [
  "https://example.com/a.pdf",
  "https://example.org/report.pdf",
  "https://example.net/notes.html",
];

const TIKTOK_SAMPLE = [
  "https://www.tiktok.com/@creator/video/7123456789",
  "https://vm.tiktok.com/ZM123abc/",
  "https://vt.tiktok.com/ZM456def/",
  "https://www.tiktok.com/t/ZM789ghi/",
];

const KICK_SAMPLE = [
  "https://kick.com/xqc/videos/5c697a87-afce-4256-b01f-3c8fe71ef5cb",
  "https://kick.com/spreen/clips/clip_01J8RGZRKHXHXXKJEHGRM932A5",
  "https://kick.com/mxddy?clip=abc123",
];

describe("A11 supported-media routing chip", () => {
  it.each([
    "ftp://youtube.com/watch?v=dQw4w9WgXcQ",
    "https://youtube.com/live/dQw4w9WgXcQ/extra",
    "https://youtube.com/embed/dQw4w9WgXcQ/extra",
  ])("does not suggest Download media for backend-rejected shape %s", (url) => {
    expect(looksLikeSupportedMediaUrl(url)).toBe(false);
  });

  it.each([
    "https://youtube.com/live/dQw4w9WgXcQ",
    "https://youtube.com/embed/dQw4w9WgXcQ",
    "https://vt.tiktok.com/ZM123abc/",
  ])("recognizes supported media shape %s", (url) => {
    expect(looksLikeSupportedMediaUrl(url)).toBe(true);
  });

  it("fetch_url: suggests Download media when the selected link column samples as YouTube URLs", async () => {
    const user = userEvent.setup();
    const { onNavigateToAction } = renderMediaAction("media.fetch_url", {
      sheet: sheetMeta([
        columnDef({ id: "1", name: "source_url", type: "link" }),
      ]),
      sampleColumnValues: (columnId) =>
        columnId === "1" ? YOUTUBE_SAMPLE : [],
    });

    const chip = screen.getByTestId("youtube-routing-chip");
    expect(chip).toHaveTextContent('"source_url"');
    expect(chip).toHaveTextContent("Download media");

    // Visible suggestion only: the normal source-column picker still renders
    // right alongside it, untouched and interactive — the chip never hides
    // or disables the form's own controls.
    expect(screen.getByTestId("field-source")).toHaveValue(
      "source_url",
    );

    await user.click(screen.getByTestId("youtube-routing-navigate"));
    expect(onNavigateToAction).toHaveBeenCalledTimes(1);
    expect(onNavigateToAction).toHaveBeenCalledWith(
      "media.ytdlp_download",
      "source_url",
    );
  });

  it("fetch_url: suggests Download media for TikTok video and share URLs", () => {
    renderMediaAction("media.fetch_url", {
      sheet: sheetMeta([
        columnDef({ id: "1", name: "tiktok_url", type: "link" }),
      ]),
      sampleColumnValues: () => TIKTOK_SAMPLE,
    });

    expect(screen.getByTestId("youtube-routing-chip")).toHaveTextContent(
      '"tiktok_url"',
    );
    expect(screen.getByTestId("youtube-routing-chip")).toHaveTextContent(
      "Download media",
    );
  });

  it("does not mistake ordinary TikTok pages for single-media URLs", () => {
    renderMediaAction("media.fetch_url", {
      sheet: sheetMeta([
        columnDef({ id: "1", name: "tiktok_page", type: "link" }),
      ]),
      sampleColumnValues: () => [
        "https://www.tiktok.com/foryou",
        "https://www.tiktok.com/explore",
        "https://m.tiktok.com/legal",
      ],
    });

    expect(
      screen.queryByTestId("youtube-routing-chip"),
    ).not.toBeInTheDocument();
  });

  it("fetch_url: suggests Download media for Kick VOD and clip URLs", () => {
    renderMediaAction("media.fetch_url", {
      sheet: sheetMeta([
        columnDef({ id: "1", name: "kick_url", type: "link" }),
      ]),
      sampleColumnValues: () => KICK_SAMPLE,
    });

    expect(screen.getByTestId("youtube-routing-chip")).toHaveTextContent(
      '"kick_url"',
    );
    expect(screen.getByTestId("youtube-routing-chip")).toHaveTextContent(
      "Download media",
    );
  });

  it("page capture has no capture-specific routing exception", () => {
    renderMediaAction("web.capture_page", {
      sheet: sheetMeta([
        columnDef({ id: "1", name: "page_url", type: "link" }),
      ]),
    });

    expect(screen.queryByTestId("youtube-routing-chip")).not.toBeInTheDocument();
  });

  it("does not render for a column whose sample is mostly non-YouTube URLs", () => {
    renderMediaAction("media.fetch_url", {
      sheet: sheetMeta([
        columnDef({ id: "1", name: "source_url", type: "link" }),
      ]),
      sampleColumnValues: () => GENERIC_SAMPLE,
    });

    expect(
      screen.queryByTestId("youtube-routing-chip"),
    ).not.toBeInTheDocument();
  });

  it("does not render when no sample is available yet (sampleColumnValues omitted) — degrades silently, never errors", () => {
    renderMediaAction("media.fetch_url", {
      sheet: sheetMeta([
        columnDef({ id: "1", name: "source_url", type: "link" }),
      ]),
    });

    expect(
      screen.queryByTestId("youtube-routing-chip"),
    ).not.toBeInTheDocument();
    // The form still renders normally — no crash from the missing sampler.
    expect(
      screen.getByTestId("field-source"),
    ).toBeInTheDocument();
  });

  it("does not render for actions other than fetch_url/capture_url even with a YouTube-looking sample", () => {
    renderMediaAction("media.ytdlp_download", {
      sheet: sheetMeta([
        columnDef({ id: "1", name: "source_url", type: "link" }),
      ]),
    });

    // download_media IS the destination the chip would suggest — it must not
    // suggest itself.
    expect(
      screen.queryByTestId("youtube-routing-chip"),
    ).not.toBeInTheDocument();
  });

  it("a majority (not unanimous) YouTube sample is still enough to suggest the redirect", () => {
    renderMediaAction("media.fetch_url", {
      sheet: sheetMeta([
        columnDef({ id: "1", name: "source_url", type: "link" }),
      ]),
      sampleColumnValues: () => [
        ...YOUTUBE_SAMPLE,
        "https://example.com/one-off.pdf",
      ],
    });

    expect(screen.getByTestId("youtube-routing-chip")).toBeInTheDocument();
  });

  it("switching the selected column to a non-YouTube-sampled one hides the chip", async () => {
    const user = userEvent.setup();
    renderMediaAction("media.fetch_url", {
      sheet: sheetMeta([
        columnDef({ id: "1", name: "yt_links", type: "link" }),
        columnDef({ id: "2", name: "other_links", type: "link" }),
      ]),
      sampleColumnValues: (columnId) =>
        columnId === "1" ? YOUTUBE_SAMPLE : GENERIC_SAMPLE,
    });

    expect(screen.getByTestId("youtube-routing-chip")).toHaveTextContent(
      '"yt_links"',
    );

    await user.selectOptions(
      screen.getByTestId("field-source"),
      "other_links",
    );
    expect(
      screen.queryByTestId("youtube-routing-chip"),
    ).not.toBeInTheDocument();
  });
});
