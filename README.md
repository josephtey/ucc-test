# ucc-test

Playing around with [Universal Constructor](https://account.universalconstructor.co/) and [ProBio](https://probio-dataset.github.io/) to see how well I can search lab videos, jump to useful moments, and spot possible operator mistakes.

A simple local UI: ask a question across videos, compare clips, and watch the evidence. There are starter prompts for things like weighing powder, mixing tubes, and placing tubes on ice. Mistake flags are suggestions to review, not verified errors.

### Run it

You'll need Python 3.12, `ffmpeg` / `ffprobe`, and a UCC API key with access to a dataset. Set up UCC's private package index using their [SDK instructions](https://account.universalconstructor.co/docs/reference.md), then:

```sh
python3.12 -m venv .venv
cp .env.example .env
# Add your UCC_API_KEY to .env
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app.py
```

Open [localhost:8765](http://127.0.0.1:8765/). On macOS, `start.command` also launches it after setup.

### The ProBio datasets

- **probio** — the videos, for general searching.
- **probio-multilevel** — the same source videos with denser frame sampling and shorter windows, for finer detail.
- **probio-annotations** — reference labels and mappings for checking results. These aren't automatically scored against predictions yet.

These need to be available in your UCC account; the repo doesn't include the data. It works with other UCC datasets too.

The UI runs locally, but queries and uploads go to UCC and may cost money. API keys, cached videos, and query history stay out of Git.

Tests: `.venv/bin/python -m unittest -v test_app.py`
