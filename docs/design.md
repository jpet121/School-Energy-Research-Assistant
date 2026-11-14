# Design notes

## The research problem

School energy reports often show several numbers in one table. A reader can confuse an estimate of energy savings with an estimate of cost savings. A note can also limit where an estimate applies.

Retrofit Evidence links a search result to the exact PDF page and region. A draft keeps those links when the researcher exports it. The researcher can check the result against the source.

## What happens to a PDF

1. The app saves the original file and records a hash. This hash identifies the exact file.
2. A separate process reads the PDF. Linux limits its processor time, memory, and network access.
3. The researcher checks the text and marks the regions that search can use.
4. The app publishes a snapshot. A snapshot records the source editions and the search index at that time.
5. The researcher adds evidence from a result to a draft. The export keeps the page, source edition, and relevant notes.

## Search and drafting

The default search uses SQLite full-text search. With local model files, the app can also search by meaning or combine both methods. The app records which search method and snapshot each run used.

Hosted generation is optional. The app checks that the user approved the exact query before it sends a request. Each model claim must link to evidence that the app supplied for that run. Those checks can reject bad references. A reviewer still decides whether the evidence supports the claim.

## Where to look in the code

| Area | File |
| --- | --- |
| Browser interface | [web/workbench.ts](../web/workbench.ts) |
| Request and session checks | [retrofit/app.py](../retrofit/app.py) |
| PDF process and limits | [retrofit/parser_worker.py](../retrofit/parser_worker.py) |
| Source review and snapshots | [retrofit/corpus.py](../retrofit/corpus.py) |
| Search | [retrofit/research.py](../retrofit/research.py) |
| Drafts and export | [retrofit/briefs.py](../retrofit/briefs.py) |
| Evaluation | [retrofit/evaluate.py](../retrofit/evaluate.py) |

## Project roles

The project owner set the scope and chose the release criteria. Codex and Factory helped build, test, and package the app. The [verification record](../evidence/distribution-verification.md) lists the checks that ran and the limits of those checks.
