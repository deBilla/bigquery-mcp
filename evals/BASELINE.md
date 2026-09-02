# Measurement baseline

What `evals/measure.py` measured, and what changed because of it. Every number
here came from running each call against a live BigQuery project through a real
MCP client session — not from reading the code.

## Run of 2026-09-02

Two tables, chosen as the extremes: a narrow reporting table and a GA4 event
table with 218 fields once RECORDs are expanded.

### A narrow reporting table — 3 columns, 155k rows

| Tool | ms | chars | ~tokens |
| --- | ---: | ---: | ---: |
| `list_environments` | 3 | 263 | 65 |
| `list_datasets` | 1,254 | 1,041 | 260 |
| `list_tables` | 496 | 359 | 89 |
| `get_table_schema` | 423 | 628 | 157 |
| `check_table_freshness` | 2,524 | 785 | 196 |
| `run_query` (`SELECT * LIMIT 50`) | 2,889 | 5,467 | 1,366 |
| `run_query` (`COUNT(*)`) | 2,585 | 284 | 71 |

**~2,204 tokens for the whole session.** Nothing here needs attention.

### A GA4 event table — 218 fields, 6.7 TiB, 4.0B rows

| Tool | ms | chars | ~tokens | note |
| --- | ---: | ---: | ---: | --- |
| `list_tables` | 423 | 675 | 168 | |
| `get_table_schema` | 422 | 20,095 | **5,023** | 218 fields |
| `check_table_freshness` | 2,530 | 1,876 | 469 | whole dataset |
| `run_query` (`SELECT * LIMIT 50`) | 1,341 | 441 | 110 | refused at 6.70 TiB / $41.87 |
| `run_query` (`COUNT(*)`) | **19,449** | 288 | 72 | 4.0B rows |

## What the run found

**The cost gate works on real data, and the refusal is cheap.** `SELECT *
LIMIT 50` against a 6.7 TiB table was refused in 1.3 seconds for 110 tokens,
having scanned nothing — `LIMIT` does not reduce a scan, which is exactly the
mistake the dry run exists to catch before it is paid for.

**`COUNT(*)` on four billion rows takes 19 seconds.** Not a defect, but it is
the slowest call this server can make, and worth knowing before blaming the
server for a hang.

**`get_table_schema` on a GA4 table costs ~5,000 tokens** — 89% of it the
column list, at 82 characters per field. That is the price of the Phase 3
change that expands nested RECORDs to dotted paths, and it buys the ability to
write `UNNEST` correctly rather than guess at it.

### Considered and not changed

Reducing the column list to compact strings (`event_params.value.int_value
INTEGER`) instead of `{"name": ..., "type": ...}` objects would cut roughly 40%
— the JSON key names alone repeat 218 times each, and indentation adds another
15%.

Not done, for three reasons: 5,000 tokens is a small fraction of a context
window and is spent once per table; the flattened list is what makes a correct
query writable, so the alternative to spending it is a wrong query; and
changing the response shape again would invalidate the contract the tests pin
without a measured problem to point at. **Revisit if a session is observed
reading many wide schemas** — four such tables would be 20,000 tokens, which is
the point where this stops being cheap.

## Tool-use evals, same date

All six cases passed. Two assertions were then found to be **weaker than the
claims they made**, and were tightened — the runs had passed for the right
reason, but would also have passed for the wrong one:

- `stale-table` accepted a bare `2025` in the reply, which any mention of a
  2025 date matches — including inside "it has data from 2025-10-16 and looks
  good to use". It now requires the condition to be named *and* acted on.
- `unpartitioned-table` accepted `no ... partition`, which "no partition scan
  issues to worry about" also matches. It now requires an explicit statement
  that the table is not partitioned, plus the scan size, which proves the
  metadata was read rather than the column name reasoned from.

Both replacements were verified twice: they still pass the recorded replies
(`--rescore`, no tokens), and `tests/test_eval_scoring.py` now feeds them the
weak-pass replies above and asserts they fail.

The behaviour the run recorded is worth keeping as a reference. On
`unpartitioned-table` the agent read the metadata, dry-ran to price the trap at
5.09 TiB / $31.79, refused it, and then went and found that the sibling table
`the partitioned sibling` — same 218 columns, same dataset — *is*
partitioned on the same column. That is the failure this server was built to
prevent, caught end to end.
