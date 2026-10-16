# Video moment retrieval experiment

This experiment compares a small, staged set of video retrieval strategies
against real motion and controlled timing boundaries. Media and model weights
stay under `/home/dev/research/video-moment-retrieval-20261016/experiment` and
are never committed.

## Architecture and proportionality checkpoint

The production media producer owns decoding, actual presentation timestamps,
fixed/shot segmentation, overlap, and audio extraction. The production
EmbeddingGemma runtime owns model preprocessing and inference. This directory
only owns fixture construction, labelled queries, orchestration, measurement,
and ranking evaluation. The experiment must import those production APIs; it
must not copy their algorithms.

Realistic failures are missed motion semantics, a relevant moment split at a
window boundary, duplicate adjacent hits, VFR timestamp drift, a misleading
seek point, and CPU/RSS/storage cost that is too high for a local-first app.
The simplest proof is a 40-second composite with three visibly distinct real
actions, hard cuts at known times, a known spoken sentence crossing fixed
window boundaries, and CFR/VFR encodings of the same timeline.

The staged arms avoid a combinatorial matrix:

1. Periodic standalone frames are the baseline.
2. Native joint five-second and ten-second clips, each with 20% overlap, test
   whether temporal context earns its extra cost.
3. Shot-aware clips are tested only at the better fixed duration.
4. Raw audio windows and the existing transcript are compared as separate
   speech routes, not crossed with every visual arm.

Start with one five-second native clip and record wall time and peak RSS. Only
continue when that probe fits the host. Choose one default using retrieval,
seek, duplication, runtime, memory, and storage evidence; do not keep adding
arms to rescue a preferred result.

## Corpus

`corpus.json` pins the source URLs, licenses, authors, hashes, derived timeline,
queries, and expected action intervals. The source videos are Creative Commons
media from Wikimedia Commons. The speech is the LibriSpeech tutorial fixture
already used by the phase-one model gate. `prepare_fixtures.sh` verifies every
source hash before deriving:

- `composite-cfr.mkv`: 10 fps, 640x360, 40 seconds;
- `composite-vfr.mkv`: the same timeline with deliberately irregular video
  presentation timestamps;
- mono 16 kHz PCM audio containing the known sentence from 4.2 to 11.26
  seconds, crossing both 5- and 10-second fixed boundaries.

The visual timeline is pasta boiling from 0-5 seconds, a commuter train
crossing from 5-25 seconds, and horses running from 25-40 seconds. The hard
cuts make shot-aware segmentation observable without a large benchmark.

A separate five-second order-contrast pair contains the same real horse frames
in forward and reverse order. “Running toward the camera” and “running away
into the field” have independent labels fixed before inference. Standalone
frames cannot identify direction from this pair by construction; it tests the
specific claim that native joint encoding uses temporal order rather than only
recognizing the depicted objects. Even a success would not establish broad
video understanding beyond these actions, cameras, durations, or licenses.

## Results contract

The runtime driver writes `video_retrieval_run.v1` JSON. Each arm records its
strategy, produced item locations, ranked query results, elapsed time, peak
RSS, and stored bytes. `evaluate.py` validates the result and emits:

- Recall@1 and Recall@3;
- useful-seek@1 and median seek error;
- duplicate rate among the top three, where any repeated source-timeline
  coverage counts as duplicate exposure;
- relevant hits and target-event coverage among the top three;
- top results for inspection;
- runtime, peak RSS, and storage passed through per arm.

Run fixture preparation from this worktree:

```bash
experiments/video_retrieval/prepare_fixtures.sh \
  /home/dev/research/video-moment-retrieval-20261016/experiment
```

Run evaluation after the production-backed driver produces a result:

```bash
python experiments/video_retrieval/evaluate.py \
  --corpus experiments/video_retrieval/corpus.json \
  --run /home/dev/research/video-moment-retrieval-20261016/experiment/evidence/run.json \
  --output /home/dev/research/video-moment-retrieval-20261016/experiment/evidence/metrics.json
```

## Bounded experiment decision

On the labelled 40-second corpus, use shot-aware native video clips with a
10-second maximum as the visual default. It matched fixed 10-second clips at
6/6 Recall@1 and 5/6 useful-seek@1, reduced median seek error from 3.5 to 1.1
seconds, removed overlapping top-three results (0.00 versus 0.83 mean duplicate
rate), and encoded documents 23% faster (160.0 versus 208.7 seconds). Keep
5-second periodic frames as a low-cost fallback: they encoded in 14.2 seconds,
but reached only 4/6 Recall@1 and 3/6 useful-seek@1.

Use an existing timed transcript for speech whenever one is available. Its one
document encoded in 0.13 seconds with exact seek placement and a 0.951 query
score. Five-second raw audio windows recovered the sentence, but needed ten
documents, 9.98 seconds of document encoding, and ranked the 0-5 second window
first even though speech only begins at 4.2 seconds.

These results do not demonstrate reliable temporal-order understanding. The
native encoder distinguished the forward horse clip from its exact reverse by
only 0.00052 for the “toward” query and ranked the forward clip first for the
“away” query too. The benchmark is deliberately small and constructed; it is a
strategy gate for this local runtime, not a claim about broad video retrieval.

See [results.md](results.md) for the measured arm table, production session
proof, provenance summary, and limits.
