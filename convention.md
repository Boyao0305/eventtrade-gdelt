# Conventions  
  
How this repository is documented and structured. Read this before writing a design document or adding a component.  
  
This file covers **what to write and where**. It does not cover tooling or checks.  
  
---  
  
## 0. Scope — read before applying anything below  
  
This describes the **full form**, sized for a system with several containers and a long life. Most projects need part of it. Applying all of it to a small project produces ceremony nobody maintains, which is worse than no convention at all.  
  
### 0.1 Two rules that override everything else  
  
**Apply a section only when its trigger is met.** Skip the rest silently — do not mention sections you skipped, do not leave placeholders, do not write "N/A".  
  
**Never create a file with nothing in it.** An empty `docs/domain.md`, or a `containers.md` holding one box and no prose, is worse than its absence: it reads as _this was considered and the answer is nothing_ when the truth is _not yet_. A file appears the first time it has real content, not before.  
  
### 0.2 Triggers  
  
| Section                                         | Create when                                                         | Otherwise                                 |  
| ----------------------------------------------- | ------------------------------------------------------------------- | ----------------------------------------- |  
| §1.2 layers, §1.3 pure core, §1.5 protocols     | always, from the first file                                         | —                                         |  
| §2 layout, `src/`, `pyproject.toml` `[project]` | more than one module                                                | a single script needs neither             |  
| §4 `containers.md`                              | **two or more** deployable processes                                | one line in the README                    |  
| §5 `components/<name>.md`                       | roughly 3 or more modules in a container                            | the import graph is small enough to read  |  
| §6 `schemas.py`                                 | a shape with two or more fields crosses a module boundary           | pass the primitives                       |  
| §7 `schema.sql`, `data-model.md`                | see §0.3                                                            | a comment in the adapter                  |  
| §8 `[tool.importlinter]`                        | three or more layers actually exist                                 | the layering is still a habit, not a rule |  
| §9 `tests/`                                     | any component whose behaviour is a judgement call, or any pure core | a script you rerun by hand                |  
| §10 ADR                                         | a decision would be expensive to reverse                            | —                                         |  
| §11 experiment                                  | a decision rests on a measurement                                   | —                                         |  
| §12 `domain.md`                                 | the problem has vocabulary or rules a newcomer would get wrong      | —                                         |  
| §14 `CLAUDE.md`, `docs/agent/`                  | work spans more than one session, or another agent will continue it | one line in the README on how to resume   |  
  
### 0.3 The database section specifically  
  
Apply §7 when **all** of these hold: the schema is migrated rather than recreated, there is more than one table with relationships between them, and more than one component reads it.  
  
Do **not** apply it for a SQLite file used as a cache, a queue, or a single-table store, nor for a handful of tables defined once by an ORM and never migrated. In those cases the table definitions live next to the adapter that uses them, and a comment naming the columns is sufficient. `data-model.md` for four tables is a diagram nobody opens.  
  
### 0.4 Rough sizes  
  
**One script.** README, and the habit of §1.3. Nothing else.  
  
**One container, a few modules.** Add `src/` layout, `pyproject.toml`, `schemas.py`, `tests/`.  
  
**One container, many modules.** Add `components/<name>.md` and the import contracts.  
  
**Several containers.** The whole file.  
  
### 0.5 When unsure  
  
Bias toward less. A convention adopted late costs an afternoon; a convention adopted early and abandoned teaches everyone that the documents are optional.  
  
State in one line which sections were applied and why, so the choice can be corrected. Do not justify the omissions.  
  
---  
  
## 1. Engineering principles  
  
### 1.1 One container, one composition root  
  
A **container** is a separately deployable process: an API server, a scheduled job, a CLI, a worker. Each container has exactly one entry point, and that entry point is the only file permitted to name concrete implementations.  
  
Everything else receives what it needs as an argument. An import of a concrete adapter anywhere outside the composition root is a defect.  
  
The entry point's job is always the same four steps: read input from the outside world, build a validated `Config`, construct the adapters, call the orchestrator.  
  
**Service (FastAPI).** `main.py` builds the `app`. Wiring goes in `lifespan`; adapters are exposed through dependency providers that return the Protocol type, never the concrete class. Route handlers parse the request and call an orchestrator — no business logic in a handler.  
  
**CLI.** `main.py` parses arguments and does nothing else. Argument parsing is a boundary: parse into a `Config` schema, validate once, pass it down. No logic in the parser, no reading of environment variables deeper in the stack.  
  
The CLI surface is a contract — flag names, defaults, exit codes. Document it in `README.md`; changing a flag is a breaking change. Subcommands map one-to-one to orchestrator functions.  
  
**Scheduled job.** Same as CLI, plus: the schedule and its trigger are container facts and belong in `containers.md`, not in the code's documentation.  
  
### 1.2 Four levels, arrows point down  
  
|Level|Contains|May import|  
|---|---|---|  
|Entry point|`main.py`|anything|  
|Orchestration|sequencing, retries, error handling, transactions|core, adapters (by Protocol), schemas|  
|Core / Adapters|pure logic \| I/O to the outside world|schemas|  
|Schemas|shared data shapes|nothing|  
  
Core and adapters are **peers**, not stacked. Neither imports the other.  
  
### 1.3 The core is pure  
  
No I/O, no network, no filesystem, no database, no clock, no randomness, no global state. Data arrives as arguments and leaves as return values.  
  
If a function needs the current time or a random seed, it takes it as a parameter. The orchestrator supplies it.  
  
### 1.4 Components do not know their mode  
  
No component branches on production versus validation, or on whether caching is on. Modes are expressed by composing different pipelines from the same components, chosen at the entry point. Caching is applied by wrapping a function, never by branching inside it.  
  
### 1.5 Protocols at the edges only  
  
Where the outside world enters — storage, network, clock, randomness — the orchestrator declares a `Protocol` describing what it needs, and the adapter satisfies it structurally. The Protocol lives in the **consumer's** file.  
  
Everywhere else, import directly. A Protocol with one implementation and no test fake is indirection without benefit.  
  
### 1.6 The unit is the module  
  
A component is a file with a small public surface, not a single function. Related helpers live beside the function they serve, prefixed with `_`.  
  
### 1.7 Read the external reference before building on it  
  
Before writing a query, a harness or a measurement against an external API, read its full reference (for Clay: `GET /search/query-mode/reference` and developers.clay.com), not an example or a previous script. Then write down, for every requirement, the field that expresses it, or that none does. A requirement no field can express is not filtered by the query and not judged afterwards. Check doubtful field names with a call that costs nothing (creating a Clay search returns 400 on an unknown field) before spending quota, credits or LLM budget. Measuring precision before this is done measures our misreading of the API, not the data (experiments 0004–0005 did exactly that).  
  
---  
  
## 2. Repository layout  
  
**One package. One subpackage per container. Everything below orchestration is shared.**  
  
```  
myproject/  
├── pyproject.toml  
├── README.md  
├── db/  
│   └── schema.sql  
├── docs/  
│   ├── containers.md  
│   ├── data-model.md  
│   ├── domain.md  
│   ├── components/  
│   │   ├── api.md  
│   │   └── cron.md  
│   ├── adr/  
│   └── experiments/  
├── src/  
│   └── myapp/  
│       ├── __init__.py  
│       ├── schemas.py          # shared vocabulary  
│       ├── config.py  
│       ├── db.py               # adapter, shared  
│       ├── core/               # pure logic, shared  
│       │   ├── __init__.py  
│       │   └── grade.py  
│       ├── api/                # container  
│       │   ├── __init__.py  
│       │   ├── main.py  
│       │   └── pipelines.py  
│       └── cron/               # container  
│           ├── __init__.py  
│           ├── main.py  
│           └── pipelines.py  
└── tests/  
```  
  
### 2.1 `src/` layout  
  
The package lives under `src/`, so it is not importable from the repository root. Work inside a virtual environment with the package installed editable:  
  
```bash  
python -m venv .venvsource .venv/bin/activatepip install -e ".[dev]"```  
  
This forces every import to resolve through the installed package rather than through the working directory, so packaging mistakes surface locally instead of on someone else's machine.  
  
### 2.2 Containers are subpackages  
  
Each container is a subpackage holding only its entry point and its orchestration. Everything it uses below that layer — core, adapters, schemas, config — lives at the package root and is shared.  
  
**Containers never import each other.** If the cron and the API both need something, it moves down into the shared layer; it does not get imported sideways.  
  
### 2.3 Schemas are shared, always  
  
When the cron writes `Item` rows and the API reads them, that is a contract between two processes, and `schemas.py` is what makes it explicit. Two copies drift: one side adds a field, the other does not know, and the failure appears in production as a missing key with no earlier warning.  
  
One definition, imported by both. This is the main reason the containers live in one package.  
  
### 2.4 Deployment  
  
The whole package ships to every container; each runs a different entry point. The cron image carries dependencies it never imports. That is acceptable.  
  
---  
  
## 3. The document set  
  
|File|Written by|Scope|Changes|  
|---|---|---|---|  
|`README.md`|hand|orientation, how to run, CLI surface|rarely|  
|`docs/containers.md`|hand|deployable units and their relationships|a few times a year|  
|`docs/components/<container>.md`|hand, then generated|modules inside one container|per feature|  
|`src/myapp/schemas.py`|hand|every cross-boundary data shape|per feature|  
|`db/schema.sql`|hand or dumped|tables, keys, indexes, constraints|per feature|  
|`docs/data-model.md`|generated|ER diagram and table notes|follows `schema.sql`|  
|`tests/`|hand|what every component does|per feature|  
|`pyproject.toml`|hand|machine-readable statement of §1.2 and §2.2|when a layer changes|  
|`docs/adr/NNNN-*.md`|hand|why a decision was made|per decision|  
|`docs/experiments/NNNN-*.md`|hand|what was measured, under what conditions|per experiment|  
|`docs/domain.md`|hand|vocabulary and rules of the problem itself|as understanding improves|  
|`CLAUDE.md`|hand|durable traps and operations that must not run without asking|as learned|  
|`docs/agent/`|agent|session log and its index|every session|  
  
Nothing outside this list is authoritative. Prose describing behaviour that lives anywhere else is a comment, not documentation.  
  
---  
  
## 4. `docs/containers.md`  
  
**Mermaid `flowchart`.** One diagram, plus one prose section per container.  
  
Include: what the container is responsible for, what it tolerates failing, its schedule or trigger, and its data stores. Exclude: anything about the modules inside it.  
  
````markdown  
# Containers  
```mermaid  
flowchart LR  
    user([User])    upstream([Upstream source])  
    subgraph system[Our system]        api[FastAPI backend<br/><i>Python — serves the query API</i>]        cron[Fetch cronjob<br/><i>Python — hourly ingest</i>]        db[(Postgres<br/>items, verdicts)]    end  
    user -->|Queries · HTTPS| api    upstream -->|Raw records · HTTPS| cron    cron -->|Writes Item| db    api -->|Reads Item, writes Verdict| db```  
  
## FastAPI backend  
Serves the query API. Stateless; horizontally scalable. Depends on Postgres  
being reachable — returns 503 if it is not.  
  
## Fetch cronjob  
Pulls from the upstream source hourly and writes `Item` rows. May be down forseveral hours without user impact; the next run backfills.  
````  
  
Mermaid has no C4 vocabulary, so the distinctions C4 encodes are carried by convention instead. Hold to these, since nothing enforces them:  
  
|Kind|Shape|Example|  
|---|---|---|  
|Person or external system|stadium|`user([User])`|  
|Container|rectangle|`api[FastAPI backend]`|  
|Datastore|cylinder|`db[(Postgres)]`|  
  
Put the technology in italics on a second line of the node label. Label every edge with what flows and over what protocol — an unlabelled arrow between containers is missing information, not a shorthand. Wrap owned containers in a `subgraph` so the system boundary is visible; anything outside it is a dependency you do not control.  
  
---  
  
## 5. `docs/components/<container>.md`  
  
**Mermaid only.** One file per container, named after the subpackage: `docs/components/api.md` documents `src/myapp/api/`.  
  
Each file shows that container's entry point and orchestration, plus the shared modules it reaches into. Shared modules therefore appear in more than one component file — that is correct, and it is how you see what depends on them.  
  
Graph first, then a list of the public surface of each module.  
  
Rules:  
  
- Nodes are modules, not functions. Edges are imports.  
- List only public functions — no leading underscore.  
- Write the **real signature**, including `| None`.  
- One line of purpose per function. If it needs a paragraph, it belongs in the module docstring.  
- No subfunctions, no internal helpers, no implementation notes.  
- Do not restate a shared module's function list in a second container's file. Document it where it is most used and reference it elsewhere.  
  
````markdown  
# Components — api  
```mermaid  
flowchart TD  
    main[api/main.py] --> pipelines[api/pipelines.py]    pipelines --> grade[core/grade.py]    pipelines --> schemas[schemas.py]    grade --> schemas    db[db.py] --> schemas```  
  
### api/main.py  
Builds the FastAPI `app`. Constructs `PostgresStore` in `lifespan`; exposes it  
through `get_store`, typed as `ItemStore`.  
  
### api/pipelines.py  
- `run_validation(store: ItemStore, run_id: RunId) -> None` — grades every item  in a run and writes the verdicts.  
Declares `ItemStore`: `fetch(run_id: RunId) -> list[Item]`,  
`write(item_id: ItemId, v: Verdict) -> None`.  
  
### core/grade.py — shared  
- `grade(answer: str, truth: str, cfg: GradeConfig) -> Verdict` — judges whether  an answer matches a non-identical truth. Pure.  
### db.py — shared  
- `PostgresStore(dsn: str)` — satisfies `ItemStore`.  
````  
  
Authorship transfers. Before a module exists, this file is hand-written and is the design. Once the module exists, the module is authoritative for its signatures and this section is regenerated from it.  
  
### 5.1 The stage view — a second graph for pipelines  
  
**Trigger:** the container is a pipeline with several inputs or several outputs, and the import graph no longer shows how data moves (every adapter hangs off `main`, so sources, transformations and outputs look alike).  
  
Add, under the import graph, one `flowchart LR` that reads left to right as the data moves:  
  
- **One subgraph per stage**, in order: where records come in, how they are transformed or enriched, how they are selected, each output, and what comes back (feedback). Name stages by what they do, not by module.  
- **Nodes are modules or adapters**, labelled with the entry-point command that triggers them. Alternatives for the same step (two providers, three sources) sit side by side in one subgraph.  
- **Edges are data hand-offs**, not imports. Label an edge with what crosses it when it is not obvious.  
- **Each output gets its own subgraph showing its composition**: the parts that build it and the shape it ends in (columns, format). Outputs differ more than inputs; that is what this graph is for.  
- **Work done outside the container** (a script run by hand, an external service) appears only as the node that receives the output. Its inside belongs in `containers.md`.  
  
Follow it with one table, one row per output: who it takes, what it needs, the file or call it produces, and who acts on it.  
  
Rules: every node names a module or command that exists; no fact appears here that is not in a module, an ADR or `containers.md`. Keep it under about 40 nodes; past that, split the stages, not the graph. Update it in the same change that adds or removes a stage.  
  
---  
  
## 6. `src/myapp/schemas.py`  
  
The shared vocabulary, used by every container. Imports nothing from the package. Holds data only — no methods that do work.  
  
Define a schema when:  
  
- the value has **more than one field**, or  
- **two components must agree** on its shape.  
  
Do not define one for a single primitive. `answer: str` is correct; `AnswerText` wrapping one string is noise.  
  
Use `NewType` for interchangeable ID types once there are three or more.  
  
```python  
from typing import NewType  
from pydantic import BaseModel  
  
RunId = NewType("RunId", str)  
ItemId = NewType("ItemId", str)  
  
  
class Item(BaseModel):  
    id: ItemId    question: str    truth: str  
  
class Verdict(BaseModel):  
    correct: bool    confidence: float    reason: str  
```  
  
Pydantic rather than dataclasses, because these are exactly the shapes crossing boundaries and arriving from untrusted sources.  
  
Types used inside a single module stay in that module. Types used by one container only still go here if they cross a component boundary — the file is the boundary vocabulary, not a shared-between-containers vocabulary.  
  
---  
  
## 7. Data model  
  
### 7.1 `db/schema.sql` is authoritative  
  
If migrations are in use, the migration history is the truth and `schema.sql` is dumped from it and committed. If not, `schema.sql` is hand-written and is the truth. Never both.  
  
Include: tables, columns with types and nullability, primary and foreign keys, indexes, check constraints, and a comment on any column whose meaning is not obvious from its name.  
  
One database is shared across containers, so a schema change affects every container that reads those tables. Note in the ADR which ones.  
  
### 7.2 Tables are not schemas  
  
`Item` the pydantic model and `items` the table are different objects that may share a name. The table carries what storage needs — surrogate keys, `created_at`, soft-delete flags, indexes, denormalised columns. The model carries what crosses a component boundary.  
  
Do not assume a one-to-one mapping and do not generate one from the other. The translation is the adapter's job, and it is a large part of what an adapter is for. A field that exists only for storage must never appear in `schemas.py`; a field that exists only in transit must never become a column by default.  
  
### 7.3 `docs/data-model.md`  
  
**Mermaid `erDiagram`**, generated from `schema.sql`. A view, never authored by hand.  
  
````markdown  
```mermaid  
erDiagram  
    ITEMS ||--o{ VERDICTS : "graded by"    ITEMS {        uuid id PK        text question        text truth        timestamptz created_at    }    VERDICTS {        uuid id PK        uuid item_id FK        boolean correct        double confidence        text reason    }```  
````  
  
Prose beneath the diagram covers retention, expected row counts, and which columns are indexed and why.  
  
---  
  
## 8. `pyproject.toml`  
  
Three things live here: what the package is, what it depends on, and the machine-readable statement of §1.2 and §2.2.  
  
```toml  
[project]  
name = "myapp"  
dependencies = ["fastapi", "pydantic", "psycopg[binary]"]  
  
[project.optional-dependencies]  
dev = ["pytest", "pyright", "import-linter"]  
  
[tool.hatch.build.targets.wheel]  
packages = ["src/myapp"]  
```  
  
Declare **packages**, never individual modules — naming `myapp.core.grade` makes this file churn on every addition.  
  
```toml  
[tool.importlinter]  
root_package = "myapp"  
  
[[tool.importlinter.contracts]]  
name = "Layers inside each container"  
type = "layers"  
layers = ["main", "pipelines"]  
containers = ["myapp.api", "myapp.cron"]  
  
[[tool.importlinter.contracts]]  
name = "Shared layers"  
type = "layers"  
layers = ["myapp.core", "myapp.schemas"]  
  
[[tool.importlinter.contracts]]  
name = "Containers do not import each other"  
type = "independence"  
modules = ["myapp.api", "myapp.cron"]  
  
[[tool.importlinter.contracts]]  
name = "Shared code does not import containers"  
type = "forbidden"  
source_modules = ["myapp.core", "myapp.db", "myapp.schemas"]  
forbidden_modules = ["myapp.api", "myapp.cron"]  
  
[[tool.importlinter.contracts]]  
name = "Core does no I/O"  
type = "forbidden"  
source_modules = ["myapp.core"]  
forbidden_modules = ["myapp.db", "sqlalchemy", "httpx", "requests"]  
  
[[tool.importlinter.contracts]]  
name = "Core and adapters are independent"  
type = "independence"  
modules = ["myapp.core", "myapp.db"]  
```  
  
Adding a container means adding it to two lists. Adding a module inside one means editing nothing.  
  
Edit this when a layer or a container is added, or a dependency is newly forbidden. Frequent edits mean the structure is not settled.  
  
---  
  
## 9. `tests/`  
  
Tests are the only authoritative statement of what a component **does**. Containers and components say what things are; schemas say what flows; nothing else says what happens. Prose that tries to fill this slot drifts silently.  
  
Layout mirrors `src/myapp/`:  
  
```  
tests/  
  core/cases.py          # the case table — a specification, not a fixture  core/test_grade.py  api/test_pipelines.py  cron/test_pipelines.py  test_db.py  test_smoke.py```  
  
One kind per layer:  
  
|Layer|Test shape|Needs|  
|---|---|---|  
|Core|table of inputs and expected outputs|nothing|  
|Orchestration|fake adapter, assert what was written|the Protocol|  
|Adapters|real dependency, few tests, marked slow|a database|  
|Entry point|it starts; one endpoint answers|the app|  
  
Most tests belong in the first row. That is the payoff of a pure core, and because the core is shared, those tests cover every container at once.  
  
**Case tables are documents.** For any component whose behaviour is a set of judgement calls rather than a formula, the table of cases _is_ the specification. Write it by hand, group rows by rule, and put one comment above each group — that comment is the prose, sitting beside the examples that make it precise.  
  
**Contract tests at the seams.** Assert that each adapter satisfies its Protocol, and that what one component returns is what the next accepts. Where one container writes what another reads, test that round trip explicitly — it is the seam with no compiler watching it.  
  
**Regression tests.** Every fixed bug gets the input that caused it, permanently.  
  
**Written before implementation, not after.** Schemas, then signatures, then the case table, then delegate. If the cases cannot be written, the design is not decided yet — which is worth discovering before there is code to evaluate.  
  
Do not write exhaustive unit tests of internals. They test decisions that are about to change.  
  
---  
  
## 10. ADRs — `docs/adr/NNNN-*.md`  
  
One file per decision. Four sections: context, decision, consequences, alternatives rejected. Append-only — a decision that turns out wrong gets a new ADR superseding the old one, never an edit.  
  
Write one when a choice would be expensive to reverse, or when the next reader would reasonably ask "why not the obvious thing?"  
  
Keep them short. If a decision rests on measurements, cite the experiment rather than reproducing its data.  
  
---  
  
## 11. Experiments — `docs/experiments/NNNN-*.md`  
  
A measurement, under stated conditions, on a stated date. Immutable: the result remains true for what was measured, and a later measurement is a new file.  
  
Distinct from an ADR. The experiment records **what was observed**; the ADR records **what was chosen**. One experiment may inform several decisions, and a decision may cite several experiments.  
  
Sections: question, setup (data volume, hardware, versions, date), method, results, and what this does _not_ establish. That last section matters most — "measured at 2M rows on one machine" is not "scales to 20M".  
  
Write one for anything a decision will rest on: cost per unit at volume, latency under load, model accuracy on a held-out set, index size, throughput ceilings. Also write one when the result was _negative_, since the strongest reason not to revisit an approach is having measured it.  
  
---  
  
## 12. Domain knowledge — `docs/domain.md`  
  
Facts about the problem, not about the system. Vocabulary, business rules, conventions of the field, and the reasoning behind judgement calls that look arbitrary from inside the code.  
  
Unlike ADRs and experiments, this is a **living document** — edit it freely as understanding improves. It has no version history worth preserving because it describes something outside the repository.  
  
Two things belong here:  
  
**A glossary.** Every term used in code, defined once. If `run`, `item`, and `verdict` have specific meanings, this is where they are pinned. The names in `schemas.py` must match these terms exactly.  
  
**Domain rules.** Statements about the problem that constrain any implementation — for example, why an answer carrying an extra qualifier should count as matching, or which sources are considered authoritative when two disagree. These explain the case tables in `tests/`; the tables make them precise.  
  
Do not put architecture here. If a statement would change when the system changes, it belongs in an ADR.  
  
---  
  
## 13. Which file to change  
  
|Change|Edit|  
|---|---|  
|New deployable process|`containers.md`, a subpackage under `src/myapp/`, `pyproject.toml`|  
|New module in a container|`components/<container>.md`|  
|Something two containers both need|move it to the shared layer; never import sideways|  
|New shape crossing a boundary|`schemas.py`|  
|New table, column, or index|`db/schema.sql`; the ER diagram follows|  
|New behaviour or judgement rule|`tests/` first, then the code|  
|New CLI flag or subcommand|`README.md`|  
|New third-party dependency|`pyproject.toml` `[project]`|  
|New layer or forbidden dependency|`pyproject.toml` `[tool.importlinter]`, plus an ADR|  
|Chose between two viable designs|an ADR|  
|Measured something a decision rests on|an experiment, then cite it in the ADR|  
|Learned something about the problem|`docs/domain.md`|  
|Renamed or resignatured a function|the code; the component list follows|  
|Learned a trap that looks like a bug|`CLAUDE.md`|  
|Finished or paused a work item|`docs/agent/log.md`, then compact (§14.5)|  
|Need current operational state|the status command, not a file (§14.3)|  
  
Documents change **before** the code they describe, in the same branch.  
  
---  
  
## 14. Agent memory and handoff — `CLAUDE.md`, `docs/agent/`  
  
The documents above record what the system *is*. They do not record what a working session  
*learned* or *left in flight*. Without that, the next agent rediscovers the same traps and resumes  
work it does not know is half done.  
  
### 14.1 Three kinds, split by kind, not by age  
  
| File | Holds | Read |  
|---|---|---|  
| `CLAUDE.md` | Durable traps and rules: things that look like bugs and are not, and operations that must not run without asking. | Always; loaded automatically. |  
| `docs/agent/log.md` | The current session's record: what changed, where, why; what was tried and rejected. | When continuing that work. |  
| `docs/agent/index.md` | One line per past session: date, summary, area tags, archive file. | Always, at session start. |  
  
Past sessions move to `docs/agent/archive/YYYY-MM-DD-<topic>.md`. Read only the entries the index  
points to, chosen by area, not by recency.  
  
### 14.2 Where a lesson goes  
  
Ask: *would this still be true next month, in a different part of the work?* Yes → `CLAUDE.md`, and  
it must earn its place by being surprising and load-bearing. No → `log.md`. A decision goes to an  
ADR, a measurement to an experiment, a fact about the problem to `domain.md`; `CLAUDE.md` links to  
them rather than restating them.  
  
Always log what was tried and rejected, with the reason. A future agent that does not know an  
approach failed will try it again.  
  
### 14.3 Live state is queried, never written down  
  
Anything the system can report about itself (queue progress, what is due, balances, what ran  
last) comes from a status command, not from a document. A line in a log about current state is  
stale the moment it is written. If the system has operational state and no command to report it,  
that is the gap to fix.  
  
### 14.4 A memory is a claim, not a fact  
  
Every line was true when written. Before acting on one, verify it: a file it names exists, a flag  
it names is in the code, a state it describes matches the status command. When they disagree,  
trust what you observe and fix the memory in the same change.  
  
### 14.5 Compact on a boundary  
  
When a work item finishes, or `log.md` passes about 800 lines, and never at the end of the context  
window:  
  
1. Move any durable trap from `log.md` into `CLAUDE.md`. Do this first.  
2. Move `log.md` to `docs/agent/archive/`.  
3. Add one row to `docs/agent/index.md`. An index, not a summary: a summary is lossy and you cannot  
   tell what was dropped.  
4. Start a fresh `log.md`.