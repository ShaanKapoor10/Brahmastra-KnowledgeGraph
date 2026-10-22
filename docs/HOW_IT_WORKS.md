# How Brahmastra works

This guide walks through the architecture one layer at a time: what each part does, how
the parts fit together, and the design choices behind them. For what Brahmastra is and
how to install it, start with the [README](../README.md) and
[Getting started](GETTING_STARTED.md).

- [Architecture at a glance](#architecture-at-a-glance)
- [Storage](#storage)
- [Workspaces](#workspaces)
- [The pipeline, stage by stage](#the-pipeline-stage-by-stage)
- [Entity resolution](#entity-resolution)
- [Meetings and lectures](#meetings-and-lectures)
- [Answering questions](#answering-questions)
- [Capturing what AI sessions learn](#capturing-what-ai-sessions-learn)
- [Models and providers](#models-and-providers)
- [Operations](#operations)
- [Quality and evaluation](#quality-and-evaluation)

---

## Architecture at a glance

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/readme/hero-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="assets/readme/hero-light.svg">
    <img src="assets/readme/hero-light.svg" alt="Sources feed one knowledge graph that every AI reads and writes over MCP." width="100%">
  </picture>
</p>

Brahmastra has three layers:

1. **Sources** write notes. Sources include Notion pages, meeting and lecture
   transcripts, AI coding sessions, the codebase, and any agent calling
   `brahmastra_add_note`. A note is plain prose, and notes are the system's source of
   truth.
2. **The knowledge graph** is everything derived from the notes: typed facts, resolved
   entities, topic clusters with summaries, contradictions and statements. The pipeline
   can rebuild all of it from the notes.
3. **Interfaces** read and write the graph:
   - the MCP server, with 13 tools, 3 prompts and a guide for assistants;
   - the REST API, with 40 routes;
   - the web dashboard.

   All three go through the same storage layer (`brahmastra/db.py`), so an agent and a
   person always see the same answer.

---

## Storage

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/readme/storage-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="assets/readme/storage-light.svg">
    <img src="assets/readme/storage-light.svg" alt="Notes live in Postgres as the system of record; the graph in Neo4j is a cache rebuilt from them." width="100%">
  </picture>
</p>

Brahmastra separates data it cannot recompute from data it can:

| | what | lifecycle |
|---|---|---|
| **Source** | notes, workspaces, a person's rejection of a meeting item | the system of record; back it up |
| **Derived** | facts, the canonical-name map, clusters, the cached graph, cached model replies | rebuilt from the notes on demand |

Two settings choose where each lives:

```
NOTE_BACKEND    notes and workspaces      postgres
GRAPH_BACKEND   the derived graph         neo4j  (or sqlite)
```

`CompositeStore` routes every storage call by the kind of data it touches. The
classification is declared once, in `stores/base.py`, and a test fails if a storage method
is added without one. Choosing a graph engine therefore never puts notes at risk.

**Postgres is the system of record.** It is networked, so several machines share one
source of truth. It also provides hybrid search: `tsvector` for words and `pgvector` for
meaning, fused by reciprocal rank (K=60). Neo4j fuses its own full-text and vector
indexes with the same arithmetic, so ranking stays the same whichever engine holds the
notes. A note store that can only search by words is rejected at startup, unless you opt
in to that explicitly.

**Deletion is complete.** Deleting a note or a workspace removes its derived rows from
the graph store first, then the source rows.

**Removing is owned and audited** (`removals.py`). Admins can remove any note or
meeting; members can remove the notes they added and the meetings they uploaded. Each
removal is written to the audit log. Removing a meeting keeps the notes made from it by
default, or removes them too if asked. A note that came from Notion is also kept out of
later syncs, so it doesn't come back; the Notion page itself is never changed. After a
removal the workspace's graph is rebuilt in the background from the facts that remain,
without syncing, extracting or writing anything back, so the removed item leaves the
entities, topics and answers within a minute or two.

**Every derived row has an owner** (`ownership.py`). A ledger records which source wrote
which notes, chunks and items, with a fingerprint of each. Re-ingesting a source applies
three rules:
- write what is new;
- update what changed;
- delete what the source no longer declares.

So editing a transcript and re-ingesting it leaves exactly what the new version supports.
Re-ingesting an unchanged one writes nothing. Intent is recorded before each write, so
an interrupted run picks up where it stopped.

See also: `stores/base.py`, `stores/composite.py`, `ownership.py`,
[NEO4J_DATA_MODEL.md](NEO4J_DATA_MODEL.md).

---

## Workspaces

A workspace is an independent graph, such as personal, work or one per project. You can
create one from the dashboard, from the API (`POST /workspaces`) or from an assistant
(`brahmastra_create_workspace`).

- **Names are per workspace.** "Sarah" at work and "Sarah" at home are different people.
- **Nothing crosses a boundary.** Entity resolution, graph building, PageRank and
  clustering never cross workspaces.
- **Cross-workspace search is explicit.** You have to ask for it, and every result names
  the workspace it came from.

Isolation is enforced in three independent layers:

1. **Bound stores.** A store is bound to one workspace and adds the filter to every
   query itself, so callers never write it.
2. **Verified binding.** The store factory verifies the binding when it builds the store,
   and raises `WorkspaceBindingError` if a backend did not honour it.
3. **Guarded queries.** The Neo4j store refuses any query that touches workspace data
   without a workspace filter. A deliberately cross-workspace query must say
   `unscoped=True`.

Notion sources follow the same rule. Each workspace syncs from the Notion source it names.
The global source belongs to the home workspace only.

See also: [WORKSPACES_DESIGN.md](WORKSPACES_DESIGN.md), `workspace.py`.

---

## The pipeline, stage by stage

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/readme/pipeline-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="assets/readme/pipeline-light.svg">
    <img src="assets/readme/pipeline-light.svg" alt="Seven incremental stages: sync, extract, resolve, build graph, summarise, cache and write back." width="100%">
  </picture>
</p>

`run_pipeline()` runs seven stages. It reports a `status` (`ok`, `partial` or `error`) and
lists any stages that failed.

**1. Sync.** Pull changed Notion pages into notes. A target can be a database or a page,
and both generations of the Notion API are supported.

**2. Extract.** A model reads each new or changed note and returns facts. Each fact has a
subject, a relation, an object, the types of both ends, a confidence and the exact quote
it came from. Relations come from a fixed vocabulary of 18, defined in `ontology.py`. A
fixed vocabulary is what lets the graph answer "who owns X?" and detect contradictions.

- **Nothing is discarded.** Common phrasings are mapped onto the vocabulary ("works at"
  becomes `employed_by`), and inverse phrasings are flipped ("Mei manages Sarah" becomes
  `Sarah reports_to Mei`). Anything that still doesn't fit is kept as `related_to`.
  Every such mapping is reported, so the vocabulary grows from evidence.
- **Code identifiers are typed by their spelling.** `snake_case`, `CONSTANT_CASE` and
  `call()` names become `code_symbol` entities.
- **Model replies are cached** under the note, the model and the prompt. Rebuilding an
  unchanged graph costs no model calls, and repeated rebuilds give the same graph.
  Changing the vocabulary correctly triggers re-extraction.

**3. Resolve.** Merge every name that refers to the same thing. See
[Entity resolution](#entity-resolution).

**4. Build graph.** NetworkX builds the graph and computes four things:

- **Centrality**, with PageRank.
- **Topic clusters**, with seeded Louvain over entities. Statements and meeting items
  then join the topic of what they mention.
- **Contradictions.** Five relations hold one value at a time: `reports_to`,
  `has_status`, `scheduled_for`, `located_in` and `employed_by`. A second value is a
  conflict, resolved to the most recent note by when it was written. Every value stays as
  evidence. When the notes carry no dates, the result is "unresolved".
- **Predicted links**, from shared neighbours.

**5. Summarise.** Each topic gets a 2 to 5 word label and a one-sentence summary. A
summary is reused for as long as the topic's membership is unchanged.

**6. Cache.** The computed graph is stored for fast serving.

**7. Write back.** Each Notion page gets one "Brahmastra Insights" toggle, with its
relationships, contradictions and suggested links. It is updated in place on every run.

Runs are locked per store, so different workspaces can rebuild at the same time. A
crashed run's lock expires after 15 minutes.

See also: `pipeline.py`, `extraction.py`, `concept_graph.py`,
[ONTOLOGY_DESIGN.md](ONTOLOGY_DESIGN.md).

---

## Entity resolution

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/readme/resolution-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="assets/readme/resolution-light.svg">
    <img src="assets/readme/resolution-light.svg" alt="Sarah, Sarah K. and Sarah Khan merge; look-alikes are kept apart." width="100%">
  </picture>
</p>

`entity_resolution.py` decides which mentions name the same entity, in four steps:

1. **Find candidates.** Five signals find possible matches:
   - exact matches;
   - one name's words contained in another's;
   - acronyms;
   - Jaro-Winkler similarity of spelling;
   - similarity of meaning, from `all-MiniLM-L6-v2` embeddings.

   On large graphs, a blocking step narrows the pairs first without changing the result.
2. **Apply safeguards.** Rules on spelling refuse look-alikes:
   - a product and its versioned release (`Brahmastra`, `Brahmastra v3`);
   - names that differ only in a number;
   - opposites (`global`, `local`);
   - a file path and the thing it implements;
   - a negation.
3. **Confirm with a judge.** A second model confirms candidate pairs. It sees each name
   alongside sentences where it was used, which lets it tell "X" from "X, the thing that
   loads it".
4. **Union and check coherence.** Merges are combined with Union-Find, and then each
   cluster is checked as a whole. A chain of merges can never join two names a safeguard
   keeps apart.

**Identities are stable.** Cluster ids are derived from their members, so repeated runs
over the same notes give the same entities. A name that has already been chosen keeps its
place as the graph grows. Only a strictly fuller form replaces it, so `Sarah` can become
`Sarah Khan`, but a person's name is never replaced by a handle. Incremental resolution
reuses every earlier decision whose inputs did not change.

See also: `entity_resolution.py`, `entity_confirm.py`, `resolution_cache.py`.

---

## Meetings and lectures

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/readme/meetings-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="assets/readme/meetings-light.svg">
    <img src="assets/readme/meetings-light.svg" alt="Quoted transcript lines become a decision, an action item with a status, and a risk, each tied to its speaker and the meeting." width="100%">
  </picture>
</p>

`brahmastra/ingest/` turns a transcript into knowledge, in eight steps:

1. **Parse** (`segment.py`). Accepts plain `Speaker:` lines, SRT, and WebVTT, including
   Teams' `<v Name>` voice tags. Consecutive lines from one speaker are joined into whole
   sentences.
2. **Name the speakers** (`speakers.py`). Diarizer labels such as `Speaker A` become real
   names, and only from evidence in the transcript:
   - a self-introduction;
   - a name that occurs in the transcript, backed by a verbatim quote.

   A voice that can't be identified stays anonymous and is never made an owner.
3. **Choose a mode** (`modes.py`).
   - A **meeting** yields decisions, action items, risks and open questions.
   - A **lecture** yields key teaching points, the audience's questions with their
     answers, and follow-ups.

   A session can be reprocessed in the other mode.
4. **Read** (`reader.py`, `comprehend.py`). The transcript is read part by part.
   - Meetings get focused passes for commitments and for concerns.
   - Every mode also gets a notes pass: a topic and key points for each part. **Every
     point must quote the transcript, or it is dropped.**
   - Each part is read twice, with the earlier part's notes as context, and the two
     readings are merged.
5. **Consolidate.** Duplicates across parts are merged. In a lecture, questions the
   presenter asked the room are set aside, because they are teaching, not audience
   questions.
6. **Track status** (`status.py`). Each action item is `open`, `done` or `blocked`, decided
   from what was said later. Done and blocked must quote the words that say so.
7. **Write to the graph** (`graph_record.py`).
   - Items become typed nodes linked by `decided_by`, `assigned_to`, `raised_by`,
     `asked_by`, `attended` and `discussed_in`.
   - Every key point becomes a **statement** node, with `said_by` and `mentions`, so the
     graph holds the substance of what was said and not only its summary.
8. **Summarise and index.** An overview (headline, summary, themes) is written for the
   session. The raw transcript is indexed as **passages**: what was said, with speaker and
   time, and with secrets redacted.

The dashboard shows each session's progress part by part while it is processed. If a
person rejects an item, the rejection is kept as source data, so it survives
reprocessing.

See also: `ingest/assemble.py` for the whole flow.

---

## Answering questions

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/readme/ask-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="assets/readme/ask-light.svg">
    <img src="assets/readme/ask-light.svg" alt="Graph first; transcript passages only as a labelled fallback." width="100%">
  </picture>
</p>

`POST /ask` answers **from the graph first** (`rag.py`):

1. **Find the starting points.** Entities are matched by name, and statements by
   meaning.
2. **Collect the surrounding facts.** One hop by default, and two automatically for
   chained questions such as "who else reports to Sarah's manager?". Up to three hops
   can be requested.
3. **Answer from those facts.** The answer includes the specifics the facts carry
   (reasons, dates, owners) and cites the notes they came from.
4. **Fall back only if needed.** If the graph does not hold the answer, the raw
   transcript passages are read instead. That answer is marked `source: "transcript"`,
   and the dashboard shows that it was read from the transcript rather than the graph.

Questions that ask for the big picture ("what are the main themes?") are answered from the
topic summaries.

`GET /paths?source=X&target=Y` finds the shortest connection between two entities, with
each hop stated as it was recorded.

See also: `rag.py`, `routers/`.

---

## Capturing what AI sessions learn

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/readme/checkpoint-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="assets/readme/checkpoint-light.svg">
    <img src="assets/readme/checkpoint-light.svg" alt="Capture to disk, distil, verify, store or keep queued; raw transcript indexed alongside." width="100%">
  </picture>
</p>

Assistants store notes as they work, through `brahmastra_add_note`. Three mechanisms
make sure nothing is lost in between.

**Session checkpoints** (`checkpoint.py`) run on Claude Code's `Stop`, `PreCompact` and
`SessionEnd` hooks, in two phases:

- **Capture** copies the new part of the conversation to a queue on disk. It does no
  network calls, so it never slows the assistant down. It reads from where it last
  stopped, so it costs the same however long the session runs.
- **Drain** distils the queue into a note and verifies it before storing it:
  - commit hashes and filenames must appear in the conversation;
  - the note must be a factual record, not invented dialogue;
  - anything doubtful is rejected and stays queued to be retried.

  The pipeline drains any backlog before it extracts.

**Session search** (`sessions.py`, `brahmastra_search_sessions`) indexes every exchange
verbatim, with secrets redacted. An exchange is a request plus the work that answered it.
Search fuses full-text and vector ranking, so a summary is never the only record.

**Code search** (`code_index.py`, `brahmastra_search_code`) indexes Git-tracked files,
also with secrets redacted.
- Python is split at definitions, TypeScript at top-level declarations, and Markdown at
  headings.
- Every chunk keeps its line range.
- Moved code is re-lined, not re-embedded.
- Searches cover source code by default, so tests and docs don't outrank the code they
  describe.

See also: [CHECKPOINTING_DESIGN.md](CHECKPOINTING_DESIGN.md).

---

## Models and providers

`llm.py` chooses the model provider for every task: extraction, resolution, summaries,
answers, meetings and checkpoints.

- **Providers.** Groq is the default. OpenAI, Anthropic, Gemini and local Ollama models
  are available through the same interface, chosen with `LLM_PROVIDER`. A configured key
  makes a provider available as a fallback, but never switches the active model on its
  own.
- **Several keys.** `GROQ_API_KEYS` holds a list of keys, which `groq_pool.py` rotates.
  A key that reaches its daily limit rests until it resets. A per-minute limit is waited
  out for exactly the delay the provider states. Keys can be managed from the Diagnostics
  tab, and a full key is never shown.
- **Failures are told apart.** An exhausted daily quota stops the run immediately. An
  oversized request fails only its own note. A retired model is reported with the setting
  to change. Notes that fail are retried automatically on the next run.
- **A separate judge model.** Entity resolution uses its own model, so confirming merges
  never draws on extraction's quota.

---

## Operations

```sh
docker compose up -d --build                    # the stack, from the repository root
docker compose --profile scheduler up -d        # keep every workspace current
```

- **Diagnostics** (`GET /diagnostics`, and the dashboard tab) shows what is queued, what
  failed and whether it will recover, with one-click retries.
- **Version checks.** `/health` and every pipeline result carry a fingerprint of the
  running code. `python -m brahmastra.version --against http://localhost:8001` compares a
  checkout with a running server.
- **Health probes.** `/health` reports liveness and `/health/ready` reports readiness,
  including which store holds the system of record.
- **Keepalive.** The `keepalive` service keeps a free-tier Neo4j Aura instance from
  suspending when it is idle.
- **Sandboxed scripts.** `python -m brahmastra.scratch -c "..."` runs a script against a
  temporary database instead of your real one.
- **Authentication.** The API requires `BRAHMASTRA_API_KEY`. The only exception is when
  `BRAHMASTRA_ALLOW_ANONYMOUS=1` is set explicitly for local use.

See also: [DEPLOYMENT.md](DEPLOYMENT.md), [PLATFORMS.md](PLATFORMS.md).

---

## Quality and evaluation

Brahmastra ships with the tools used to evaluate it, so every change to extraction,
resolution or comprehension can be measured before it is adopted:

| tool | evaluates |
|---|---|
| `python -m brahmastra.ingest.evaluate` | meeting comprehension against labelled cases, including traps it must avoid |
| `python -m brahmastra.ingest.qa_eval` | a session end to end, through to `/ask`, with answers graded by a second model |
| `python -m brahmastra.resolution_eval` | a merge judge against labelled entity pairs |
| `python -m brahmastra.coercions` | where extraction strains the vocabulary |

The [roadmap](ROADMAP.md) records what is planned next and the evidence behind each item.
