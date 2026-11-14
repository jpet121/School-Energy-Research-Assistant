# Run Retrofit Evidence locally

The app runs in a Linux container. You use it in your web browser. Use Docker Desktop on Windows or macOS. On Linux, use Docker Engine.

## Start from GitHub

1. Clone the repository.
2. Start Docker.
3. Open a terminal in the repository folder.
4. Run the start command for your system.
5. Open `http://127.0.0.1:8765` in your browser.

**Windows**

```powershell
.\platform\Start-RetrofitEvidence.ps1
```

Use Linux containers in Docker Desktop. On the first start, Docker builds the image from the files in the repository.

**macOS and Linux**

```bash
./deploy/start-local.sh
```

## Start from a ZIP file

The [GitHub releases](https://github.com/jpet121/School-Energy-Research-Assistant/releases) contain ZIP files for each processor type.

1. Download the ZIP for your processor. AMD64 is for Intel or AMD. ARM64 is for Apple Silicon or ARM Linux.
2. Extract the ZIP.
3. Start Docker.
4. Open the start file in the extracted folder.

| System | Start file |
| --- | --- |
| Windows | `Start Retrofit Evidence.cmd` |
| macOS | `Start Retrofit Evidence.command` |
| Linux | `start-linux.sh` |

Each ZIP includes the Linux image. The Mac launchers still await testing on a Mac.

## Try the sample

The [sample PDF](../examples/school-energy-sample.pdf) contains made-up data. It gives separate estimates for energy savings and cost savings. A note explains what the model excludes.

1. Open **Sources**.
2. Select the sample PDF.
3. Complete the source fields with the values below.
4. Select **Stage PDF**.
5. Select **Open staged source**.
6. Select **Extract PDF**.
7. Open the original page in **Extraction review**.
8. Compare the extracted text with the page.
9. Close the original page.
10. Select the lines below the title.
11. Enter a note about your comparison.
12. Select **Save extraction review**.
13. Return to **Sources**.
14. Select the sample source.
15. Select **Prepare snapshot**. The default setting uses full-text search.
16. Select **Publish snapshot**.
17. Open **Research**.
18. Enter `22.2` in the search box.
19. Select **Search evidence**.
20. Select **Inspect evidence** for the result that shows energy use.
21. Select **Add qualified note to draft**.
22. Open **Drafts**.
23. Select the file format.
24. Select **Prepare download**.
25. Select **Download saved draft**.

| Source field | Sample value |
| --- | --- |
| Document title | Sample school energy report |
| Publisher | Retrofit Evidence |
| Source URL | `https://example.org/school-energy-sample.pdf` |
| Publication date | `2026-11-14` |
| Research scope | Sample school energy figures |
| Permitted use | Demonstration and testing |
| Public document | Selected |

The source URL is a placeholder for this sample. The app uses the local file. [See a saved export](../examples/sample-draft.md)

## Stop the app

On Windows, use the stop command in PowerShell.

```powershell
.\platform\Start-RetrofitEvidence.ps1 -Stop
```

On macOS or Linux, use the stop command in your terminal.

```bash
./deploy/start-local.sh stop
```

Docker keeps your data in the `retrofit-evidence-desktop` volume. Keep this volume when you update the app. The portable app uses its own store.
