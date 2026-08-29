# BUILD + LOW greeting smoke

This example performs one explicitly authorized live Codex Builder attempt in a disposable Git repository. The frozen Task allows only `greeting.py` to change and validates it with `python3 -m unittest -v`. BUILD + LOW routes directly through the restricted Builder, AutoDev validation, path policy, checkpoint, and evidence gates; it does not launch a Reviewer.

Run from the repository root only when one live model call and its token cost are authorized:

```bash
PYTHONPATH=src python3 examples/build-low-greeting/run_live_smoke.py
```

The script sets `AUTODEV_LIVE_CODEX=1` only for its bounded `autodev run --task TASK-001` child. BUILD uses the Codex `:workspace` permission profile. On Linux, AutoDev first runs a no-model sandbox preflight and stops before Task claim or model invocation if the runtime cannot enforce it. Sandbox backend selection belongs to Codex; this example has no backend or fallback option.

The disposable project sets its infrastructure retry budget to zero, so one script invocation can launch at most one Codex Builder process.

The script prints a sanitized result containing versions, hashes, validation status, and routing counts. On success it removes the temporary project and its raw Codex JSONL; on failure it preserves the project and prints its location. Use `--keep` to retain a successful project for local inspection. Never commit the retained `.autodev/runs/**` transcript.

The accepted smoke result in `smoke-result.json` records one Builder, zero Reviewers, only `greeting.py` changed, successful validation, Task `ACCEPTED`, and Project `COMPLETE`. It contains no model transcript or credentials.
