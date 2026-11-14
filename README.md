# Retrofit Evidence | School Energy Research Assistant

**Check where a school energy savings claim came from before you use it in a recommendation.**

School energy reports can show energy savings and cost savings side by side. When a brief drops the heading or footnote, a reviewer can mistake one figure for the other.

Retrofit Evidence helps facilities teams find evidence in public PDFs and check it on the original page. When you add a result to a draft, the export keeps its source, page, and any linked notes.

**Example.** The sample report shows a 22.2% reduction in energy use and a 21.3% reduction in energy cost. Both figures are estimates from a model. The screenshot shows where the energy-use figure came from. The exported draft lets a reviewer trace it back to that page.

![The app highlights the 22.2% energy-use figure on the original sample page](docs/images/evidence-view.png)

*Screenshot from the working app with a sample PDF.*

**[Try the sample](docs/run-locally.md#try-the-sample)** · **[See an exported draft](examples/sample-draft.md)** · **[See how the app keeps source links](docs/design.md)** · **[View test results](evidence/distribution-verification.md)**

## What the app does

- Add a public PDF. Record its publisher, source link, and terms of use.
- Review the extracted text against the original page. Select the parts that search can use.
- Search by word or phrase. Open the cited page to check a result and its notes.
- Add evidence to a draft. Export the draft as Markdown or JSON.

Search and drafting run locally without a paid model. Optional model files enable search by meaning and search that combines both methods.

## Run it

Use Docker with Linux containers. The app opens in your browser at `http://127.0.0.1:8765`.

1. Clone this repository.
2. Start Docker.
3. Run the command for your system from the repository folder.

| System | Command |
| --- | --- |
| Windows | `.\platform\Start-RetrofitEvidence.ps1` |
| macOS or Linux | `./deploy/start-local.sh` |

Windows Chrome and Linux on Intel, AMD, and ARM passed the local workflow tests. The Mac launchers still await testing on a Mac. [Setup, downloads, and stop commands](docs/run-locally.md)

## How it works

Python and FastAPI handle file intake, search, and export. TypeScript runs the browser interface. SQLite stores document records and supports full-text search. The PDF parser runs in a separate Linux process with limits on memory, run time, and network access.

## What the tests show

The test suite checks PDF intake, page review, search, and export. Browser tests cover keyboard use and zoom. The HTTPS setup passed a local test. [Results and test scope](evidence/distribution-verification.md)

The app starts with hosted answer generation disabled. The starter collection and generated-answer quality still await formal review. Exports are drafts for review.
