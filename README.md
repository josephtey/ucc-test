# ucc-test

A local video workspace and chat for corpora in your Universal Constructor account. Search across multiple videos, compare findings, and play the source evidence at its indexed timestamps. The dataset selector discovers corpora available to your API key, including ProBio if your account has access. No datasets or credentials are included in this repository.

## Start

Use Python 3.12 and install `ffmpeg` (which includes `ffprobe`) for video playback. Create a virtual environment and a local configuration file:

```sh
python3.12 -m venv .venv
cp .env.example .env
```

Set `UCC_API_KEY` in `.env`. Configure access to UCC's authenticated package index using the [official SDK installation instructions](https://account.universalconstructor.co/docs/reference.md), then install the pinned dependencies and start the app:

```sh
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app.py
```

Open http://127.0.0.1:8765. You can also double-click `start.command` on macOS. Stop the server with Ctrl+C in its terminal. Set `PORT` to change the port.

The UCC SDK requires access to its private package index; a standard PyPI-only installation may not find it. Keep package-index credentials and API keys out of tracked files. The server reads its API key from `.env`, which is ignored by Git.

## Explore

- Choose a dataset, filter filenames or metadata, and open a file. The full file index loads progressively; use **Show more** to browse more rows.
- Videos play in an overlay with a synchronized timeline and evidence sidebar. Unsupported codecs are converted locally to H.264/AAC for browser playback; original files are preserved. This requires `ffmpeg` and `ffprobe` on the server's PATH. Indexed segment buttons jump to sampled time ranges. Frame sampling settings appear in **File details**. Thumbnails come from the prepared source frames. Unqueried segments have time ranges, not invented captions.
- **Describe each** produces a short C1 answer for every prepared segment in the chosen scope.
- **Find clips** searches across multiple videos, ranks their segments against a visual description, and shows clickable evidence thumbnails. Scores are model judgments, not calibrated confidence.
- **Ask & compare** starts Napa with the selected files, filtered files, an explicit 20-file subset, or the entire dataset, and displays its answer, comparison tables, findings, and citations. Follow-ups continue a completed Napa conversation when the scope and selected files are unchanged. Clicking a source opens its file. Video sources jump to the cited segment's first sampled frame, not necessarily an exact action boundary.
- **History** reopens results in the chat. Napa polling can resume after a page reload or server restart. Interrupted C1 requests are marked explicitly rather than resubmitted.
- **Export JSON** saves results with the question, corpus, file IDs, scope, and source metadata.
- **New dataset** creates a persistent UCC corpus. **Add files** uploads selected files to UCC, up to 64 MiB each. Refresh after preparation completes. The interface does not delete corpora or files.

C1 searches process every prepared segment in the chosen scope in batches of 100, with progress and cancellation between batches. There is no silent 20-file or 100-segment cutoff. The entire dataset is the default scope; checkboxes allow a custom selection, and the filter and explicit first-20 options provide smaller scopes. The first-20 option is a deterministic selection, not a random representative sample. Match results show the best segment from each of the top 20 matching videos; exported JSON retains all segment scores. Supporting descriptions are generated for the best segment of those top videos. Different video levels may overlap; rows are segments, not unique videos. A full-dataset Napa run is available explicitly in the scope menu.

ProBio source annotations are a separate corpus. Switch to `probio-annotations` to inspect them; this version does not automatically align labels with video predictions.

## Local data and limits

The server binds only to 127.0.0.1. The API key stays on the Python server; it is never placed in browser JavaScript. Model queries and uploads still use the remote UCC API and may incur usage charges. This is a local development tool, not a public hosted service.

Viewed media is cached on demand in `.cache/media/` for byte-range video seeking. Query history is saved in `.cache/jobs/`. These directories are ignored by Git. Media is retained locally until you remove the cache; removing local files does not delete remote corpora. Original media may download before playback starts. Text previews are limited to 200 KB. Images, videos, and PDFs use native browser viewers; other documents show UCC's prepared text.

## Verify

```sh
.venv/bin/python -m unittest -v test_app.py
```

Tests cover request boundaries, input limits, key redaction, media byte ranges, legacy video conversion and caching, and multi-video searches spanning more than two batches without making UCC calls.
