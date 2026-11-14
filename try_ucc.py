"""Small, resumable UCC trial using fictional documents only.

Run with .venv/bin/python try_ucc.py. Results and remote IDs are saved locally.
The demo corpus expires after one hour. Delete results/state.json for a new run.
"""

import json
import time
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from ucc import Choice, Client, Completion, Facet

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "results"
DOCUMENTS = [
    {
        "name": "retry.txt",
        "text": "The agent ran pytest. It failed because requests was missing. The agent ran pytest again without installing requests. It failed again. The agent installed requests, ran pytest, and all tests passed.",
        "metadata": {"session": "retry", "group": "example"},
    },
    {
        "name": "fixed.txt",
        "text": "The agent ran pytest. It failed because requests was missing. The agent installed requests before running pytest again. All tests passed.",
        "metadata": {"session": "fixed", "group": "example"},
    },
]


def save(name, value):
    path = OUT / name
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, default=str) + "\n")
    temporary.replace(path)


def main():
    load_dotenv(ROOT / ".env")
    OUT.mkdir(exist_ok=True)
    state_path = OUT / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {
        "corpus": f"codex-ucc-trial-{uuid4().hex[:8]}",
        "upload_key": str(uuid4()),
        "response_key": str(uuid4()),
    }
    save("state.json", state)
    save("inputs.json", DOCUMENTS)
    with Client() as client:
        api = client.with_options(corpus=state["corpus"])
        if not state.get("created"):
            api.corpora.create(state["corpus"], keep_alive=3600)
            state["created"] = True
            save("state.json", state)
            print("Created corpus:", state["corpus"], flush=True)
        if "file_ids" not in state:
            files = api.documents.add(DOCUMENTS, idempotency_key=state["upload_key"])
            state["file_ids"] = [file["id"] for file in files]
            save("state.json", state)
        deadline = time.monotonic() + 600
        for file_id in state["file_ids"]:
            previous = None
            while True:
                file = api.files.retrieve(file_id)
                details = file.model_dump().get("ucc", {})
                status = details.get("status")
                if status != previous:
                    print(f"File {file_id}: {status}", flush=True)
                    previous = status
                if status == "ready":
                    break
                if status == "failed" or file.status == "error":
                    raise RuntimeError(f"Preparation failed: {file_id}: {details}")
                if time.monotonic() >= deadline:
                    raise TimeoutError("Files still preparing; rerun to resume.")
                time.sleep(3)
        if not (OUT / "c1.json").exists() and not (OUT / "c1-error.json").exists():
            print("Running C1 document queries...", flush=True)
            started = time.monotonic()
            try:
                rows = client.corpus(state["corpus"]).select(
                    "session",
                    repeated_failure=Facet("Does the agent repeat a failing command without first changing anything?"),
                    outcome=Choice("How does this session end?", ["tests pass", "tests fail", "unclear"]),
                    evidence=Completion("Quote the action taken immediately after the first failed test run.", max_tokens=120),
                )
                rows.save(OUT / "first-table.parquet")
                save("c1.json", {"elapsed_seconds": time.monotonic() - started, "rows": rows.to_dicts()})
                for row in rows:
                    print(row.session, row.repeated_failure, row.outcome.choice, row.evidence, flush=True)
                by_session = {row.session: row for row in rows}
                checks = {
                    "two_rows": len(rows) == 2,
                    "retry_scores_higher": by_session["retry"].repeated_failure > by_session["fixed"].repeated_failure,
                    "both_end_with_passing_tests": all(row.outcome.choice == "tests pass" for row in rows),
                }
                save("c1-checks.json", checks)
                print("C1 checks:", checks, flush=True)
            except Exception as error:
                save("c1-error.json", {"type": type(error).__name__, "detail": str(error)})
                print("C1 error:", type(error).__name__, str(error), flush=True)
        if "response_id" not in state:
            response = api.responses.create(
                model="napa",
                input="Compare the two sessions. Why did the unchanged retry fail in retry.txt, what fixed the issue, and how did fixed.txt differ? Be concise and cite the source evidence.",
                extra_body={"tools": [{"type": "citations"}]},
                extra_headers={"Idempotency-Key": state["response_key"]},
                background=True,
            )
            state["response_id"] = response.id
            save("state.json", state)
            print("Napa response:", response.id, flush=True)
        deadline = time.monotonic() + 900
        previous = None
        while True:
            response = api.responses.retrieve(state["response_id"])
            # UCC's citations tool extends the OpenAI tool union.
            save("napa.json", response.model_dump(warnings=False))
            if response.status != previous:
                print("Napa status:", response.status, flush=True)
                previous = response.status
            if response.status not in ("queued", "in_progress"):
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("Napa still running; rerun to resume the same response.")
            time.sleep(5)
        if response.status != "completed":
            raise RuntimeError(f"Napa {response.status}: {response.error}")
        (OUT / "answer.txt").write_text(response.output_text + "\n")
        print(response.output_text, flush=True)
        citations = response.model_dump(warnings=False).get("ucc", {}).get("citations", [])
        texts = {doc["name"]: doc["text"] for doc in DOCUMENTS}
        checks = [
            {"filename": citation.get("filename"), "quote": citation.get("quote"),
             "exact_source_match": bool(citation.get("quote")) and citation["quote"] in texts.get(citation.get("filename"), "")}
            for citation in citations
        ]
        save("citation-checks.json", checks)
        print("Citation checks:", json.dumps(checks, indent=2), flush=True)


if __name__ == "__main__":
    main()
