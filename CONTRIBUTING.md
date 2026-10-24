# Contributing

Thanks for helping. This is a small Python codebase with a fast, credential-free
test suite, so a first contribution can go from clone to green CI in a few
minutes.

**New here?** Start with an issue labelled
[`good first issue`](https://github.com/deBilla/bigquery-mcp/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22).
Each one names the file to change, what "done" looks like, and how to test it.
Comment on the issue to say you are taking it, so two people do not do the same
work. If you have not opened a PR within a week, it is fair game again.

---

## Setup

You do **not** need a Google Cloud account, BigQuery access or credentials to
work on this repository. Every test runs against fakes.

```bash
git clone https://github.com/<you>/bigquery-mcp.git
cd bigquery-mcp
python3 -m venv .venv                 # Python 3.11, 3.12 or 3.13
./.venv/bin/pip install -e ".[dev]"
./.venv/bin/python -m pytest          # ~270 tests, under a minute
```

If the suite passes, you are ready.

---

## Where things live

| Path | What it is |
|------|------------|
| `src/data_platform_mcp/server.py` | Entry point, CLI (`serve` / `doctor` / `setup`) and the server instructions sent to the AI client |
| `src/data_platform_mcp/tools/` | One module per tool family. Each tool is a plain function with a docstring — **the docstring is what the AI reads**, so it is behaviour, not documentation |
| `src/data_platform_mcp/registration.py` | Attaches every tool with read-only annotations and audit logging |
| `src/data_platform_mcp/errors.py` | Turns Google/BigQuery exceptions into messages that tell the user how to fix them |
| `src/data_platform_mcp/formatting.py` | Byte and cost rendering for the numbers a user is asked to approve |
| `src/data_platform_mcp/config.py` | Environments, the TOML config file and env vars |
| `src/data_platform_mcp/diagnostics.py` | `data-platform-mcp doctor` |
| `tests/conftest.py` | The fake BigQuery, Data Transfer, Dataform and Vertex AI clients every test uses |
| `evals/` | Live-credential measurement and tool-use evals — **not** needed for most changes |

---

## Rules that CI enforces

These are not style preferences; a PR that breaks one fails CI.

1. **Read-only, always.** The server never writes, deletes or triggers anything.
   CI greps `src/` for mutating calls on BigQuery, Data Transfer, Dataform, and
   for any HTTP verb other than GET. If you think you need one, open an issue
   first — the answer is almost certainly no.
2. **No test touches the network.** CI runs with no GCP credentials. If a test
   only passes with your own login, it is talking to BigQuery; extend the fakes
   in `tests/conftest.py` instead.
3. **Adding or removing a tool means updating two lists.** `EXPECTED_TOOLS` in
   `tests/test_protocol.py`, and the literal tool count in the `package` job of
   `.github/workflows/ci.yml`. The literal is deliberate — it checks the built
   wheel, not the source.
4. **No secrets, no local config.** `.env` and `.mcp.json` must stay untracked,
   and CI scans for credential-shaped strings. Use placeholders such as
   `your-gcp-project` in docs and examples.

---

## Writing code here

- **Match the surrounding code.** Comments explain *why* — usually the failure
  a line prevents — not what the line does. Read a neighbouring function before
  writing yours.
- **Errors are raised, not returned.** A refusal returned as `{"error": ...}`
  arrives at the client looking like a successful result. Raise
  `DataPlatformMCPError` with a message that tells the user what to do next.
- **Error messages are read by an AI and relayed to a person.** Name the
  environment, the identity, and the exact command or role that fixes it.
- **Every behaviour change gets a test.** Name the test after the behaviour,
  e.g. `test_a_nonzero_scan_is_never_priced_at_exactly_zero`, and say in the
  docstring which defect it guards against if there was one.
- **Keep dependencies out.** A new runtime dependency needs a strong reason;
  Vertex AI is reached over plain REST precisely to avoid one.

---

## Opening a pull request

1. Fork, then branch from `main` (`fix/quota-error-message`, `docs/changelog`, …).
2. Make the change and run `./.venv/bin/python -m pytest`.
3. Open the PR against `main`. Link the issue with `Fixes #123` so it closes on
   merge.
4. Describe what changed and how you tested it. Screenshots of `doctor` output
   or tool responses are welcome when the change is user-visible.

Small PRs are reviewed fastest. If a change grows past a couple of hundred
lines, or touches the cost gate in `query_tools.py`, open an issue to discuss
the approach first.

You do not need to bump the version or edit `server.json` — releases are cut by
the maintainer.

---

## Reporting bugs and asking questions

- **Bug:** open an issue with the bug template. Include the
  `data-platform-mcp doctor` output, **with project IDs and emails redacted**.
- **Security problem** (a way around the read-only guarantee, credential
  exposure): do not open a public issue. Use GitHub's
  [private vulnerability reporting](https://github.com/deBilla/bigquery-mcp/security/advisories/new).
- **Question or idea:** open an issue — there is no question too small.

By contributing you agree that your work is released under the
[MIT License](LICENSE).
