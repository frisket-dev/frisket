# Video retrieval strategy result

Experiment date: 2026-10-16

## Decision

Use the confined ffmpeg scene-score v1 strategy with a 10-second maximum as the
provisional visual default. Keep fixed 10-second windows selectable when the
extra useful-seek hit matters more than duplicate results and indexing cost,
and keep independent frames every five seconds as the cheap fallback. Use an
existing timed transcript as the primary speech route; native raw-audio windows
are an opt-in fallback when no transcript is present.

The native clip result does not establish temporal-order understanding. The
controlled forward/reverse pair failed that check.

## Result

Six visual queries were labelled before inference: two static queries and four
motion/action queries. A useful seek is a relevant top result whose start is
within five seconds of the labelled seek point. Duplicate rate counts repeated
source-timeline coverage among the top three results.

| Arm | Items | Recall@1 | Recall@3 | Useful seek@1 | Median seek error | Duplicate rate@3 | Document encode | Embedding bytes |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Frames every 5s | 8 | 4/6 | 6/6 | 3/6 | 4.5s | 0.00 | 14.24s | 24,576 |
| Fixed clips 5s, stride 4s | 10 | 5/6 | 6/6 | 3/6 | 3.0s | 0.67 | 185.62s | 30,720 |
| Fixed clips 10s, stride 8s | 5 | 6/6 | 6/6 | 5/6 | 3.5s | 0.83 | 208.71s | 15,360 |
| Confined ffmpeg scene v1, max 10s | 6 | 6/6 | 6/6 | 4/6 | 1.0s | 0.00 | 160.43s | 18,432 |

The confined scene-score v1 clips matched fixed 10-second Recall, reduced
median seek error by 2.5 seconds, removed overlapping top-three results, and
reduced document encoding time by 23%. They lost one useful-seek hit (4/6
versus 5/6) and store one additional 768-dimensional float32 vector, or 3,072
bytes. Their spans were `[0,5]`, `[5,11]`, `[11,21]`, `[21,25]`, `[25,35]`, and
`[35,40]`.

The original shot result used in-process PySceneDetect and is superseded. A
fresh ffmpeg scene score threshold of 0.10 is part of the versioned strategy;
it is not presented as an equivalent conversion of PySceneDetect's score. An
untuned ffmpeg threshold of 0.27 retained 6/6 Recall but fell to 3/6 useful
seeks and a 6.0-second median error, so it was rejected before the decision.

Frames encoded more than eleven times faster than the selected default, with
two fewer Recall@1 hits and two fewer useful top seeks. This supports a real
low-cost setting rather than treating native clips as universally better.

The speech query produced the following result:

| Arm | Items | Recall@1 | Seek error | Duplicate rate@3 | Document encode | Input bytes | Top score |
|---|---:|---:|---:|---:|---:|---:|---:|
| Raw audio 5s, stride 4s | 10 | 1/1 | 4.2s | 0.50 | 9.98s | 1,600,000 | 0.8688 |
| Existing timed transcript | 1 | 1/1 | 0.0s | 0.00 | 0.13s | text | 0.9508 |

The audio model recovered the boundary-crossing sentence but ranked `[0,5]`
first even though speech begins at 4.2 seconds. The timed transcript produced
the exact seek point with one document.

For the exact horse clip and its frame reversal, native five-second encoding
scored 1/2 Recall@1. Forward beat reverse by 0.000523 for “toward,” and forward
also beat reverse by 0.000221 for “away.” The observed gains on the composite
may come from object and scene context rather than motion direction.

## Production and timing proof

The arms imported the production producer at
`63034239a5c7c85cc262c18a07ee24070ab1f860` and direct runtime at
`1d7b5f4138ed1812efde06473ab4349603d44e7c`. They used
`google/embeddinggemma-2` revision
`914f7f89142e33e77833254d9c9b90c3cef7303b`, Transformers 5.19.0, Torch
2.14.1+cpu, two Torch threads, and one model worker. Across measured direct
arms, peak RSS was 5.10-5.17 million KiB.

The final end-to-end proof used integration head `a0a8f443` plus sandbox fix
`4f4a39c` in isolated composition `ebebe9ff`; that fix is integrated upstream
at `c480fcaa`. The real producer, `EmbeddingGemma2Session`, launch/bootstrap,
sandbox process, worker, complete pinned snapshot, and clean close returned a
768-dimensional unit vector for one five-frame clip. Model startup took 7.63
seconds, encoding 18.84 seconds, and close 1.09 seconds. Process wall time was
31.02 seconds and process-tree peak RSS was 5,039,252 KiB.

The CFR and controlled VFR files produced identical one-second sample points
and fixed clip boundaries. Dense decoding saw 400 CFR frames at 0.1-second
gaps and 160 VFR frames with the intended 0.1, 0.2, and 0.5-second gaps.
Coverage was complete in both cases.

## Corpus and limits

The source media are Creative Commons files whose URLs, description pages,
licenses, authors, and SHA-256 values are pinned in `corpus.json`: pasta
boiling by Anselm Schuler (CC BY-SA 4.0), a GO Train by Wikimedia user Whpq
(CC BY-SA 3.0), horses by Field of Dreams (CC BY 3.0), and LibriSpeech sample
1688-142285-0007 (CC BY 4.0). Media and model weights are not committed.

This is one constructed 40-second corpus, six visual queries, one speech query,
and one order pair on CPU/Linux. Some action phrases can be answered from
objects or scene context in still frames. The experiment does not estimate
quality on long or diverse videos, varied editing, multiple speakers, noisy
audio, music, or broad temporal reasoning.

Machine-readable runs, metrics, `/usr/bin/time` records, ffprobe output, and
fixture hashes are retained under
`/home/dev/research/video-moment-retrieval-20261016/experiment/evidence`.
