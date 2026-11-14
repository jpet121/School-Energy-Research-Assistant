export {};

type DocumentVersion = {
  version_id: string;
  title: string;
  publisher: string;
  source_url: string;
  publication_date: string | null;
  unknown_reason: string | null;
  byte_hash: string;
};

type Ingestion = {
  job_id: string;
  state: string;
  document: DocumentVersion;
  failure_code: string | null;
  extraction: Extraction | null;
};

type Span = {
  span_id: string;
  normalized_text: string;
  pdf_page_1based: number;
  printed_page_label: string | null;
  bounding_boxes: [number, number, number, number][];
  requires_review: boolean;
  page_width: number;
  page_height: number;
  table_candidate?: number;
  candidate_row?: number;
  candidate_column?: number;
  excluded_reason?: string;
};
type Page = { pdf_page_1based: number; printed_page_label: string | null; width: number; height: number; within_use_scope: boolean; spans: Span[]; quarantined_lines: number };
type TableLinks = {
  cell_span_ids: string[];
  row_header_span_ids: string[];
  column_header_span_ids: string[];
  unit_span_ids: string[];
  mandatory_note_span_ids: string[];
  notes_absent_reason: string | null;
};
type Extraction = {
  pages: Page[];
  config_hash: string;
  review?: {
    approved_span_ids: string[]; table_rows: TableLinks[]; qa_note: string;
    normalizations?: Record<string, Span>; printed_page_labels?: Record<string, string>;
  };
};
type Mode = "lexical" | "dense" | "hybrid";
type Snapshot = { snapshot_id: string; page_count: number; chunk_count: number; published_at: string | null; enabled_modes: Mode[] };
type Bundle = {
  bundle_id: string; rendered_context: string; spans: Span[]; mandatory_qualifier_span_ids: string[];
  document?: DocumentVersion; eligible_for_new_use?: boolean; page_width?: number; page_height?: number;
};
type Run = { run_id: string; status: string; result_kind: string; bundles: Bundle[]; mode_used: Mode | null; failure_code?: string; incomplete_coverage?: boolean; snapshot_id: string; eligibility_revision: number; attempts: unknown[] };
type Quote = { source_span_id: string; text: string; bounding_boxes: [number, number, number, number][] };
type Citation = { run_id: string; bundle_id: string; source_title: string; quote?: Quote | null };
type Claim = { claim_id: string; marker: string; text: string; review_status: string; citations: Citation[] };
type Draft = { brief_id: string; revision_id: string; parent_revision_id: string | null; text: string; claims: Claim[] };

function node<T extends HTMLElement>(id: string): T {
  const element = document.getElementById(id);
  if (!element) throw new Error(`Missing workbench control ${id}`);
  return element as T;
}

function element<K extends keyof HTMLElementTagNameMap>(
  tag: K, text?: string, className?: string,
): HTMLElementTagNameMap[K] {
  const result = document.createElement(tag);
  if (text) result.textContent = text;
  if (className) result.className = className;
  return result;
}

function button(label: string, action: () => void, secondary = false): HTMLButtonElement {
  const result = element("button", label, secondary ? "secondary" : undefined);
  result.type = "button";
  result.addEventListener("click", action);
  return result;
}

const token = document.querySelector<HTMLMetaElement>('meta[name="retrofit-session"]')?.content ?? "";

async function request(path: string, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers);
  headers.set("X-Session-Token", token);
  const response = await fetch(path, { ...init, headers, cache: "no-store", redirect: "error" });
  if (!response.ok) {
    const body = await response.json().catch(() => null) as { message?: string; correlation_id?: string } | null;
    throw new Error(body?.message ?? "The local request could not be completed.");
  }
  return response;
}

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  return await (await request(path, init)).json() as T;
}

function send<T>(path: string, body: unknown = {}): Promise<T> {
  return json<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function announce(text = "", error = ""): void {
  node("status").textContent = text;
  node("error").textContent = error;
}

function showError(error: unknown): void {
  const message = error instanceof Error ? error.message : "The local operation failed.";
  if (node<HTMLDialogElement>("workbench-dialog").open) {
    node("dialog-error").textContent = message;
  } else {
    announce("", message);
  }
}

function dateLabel(source: DocumentVersion): string {
  return source.publication_date ?? `Date unknown (${source.unknown_reason ?? "no date recorded"})`;
}

function publisherLink(source: DocumentVersion): HTMLElement {
  const link = element("a", "Open publisher source (new tab)");
  const url = new URL(source.source_url);
  if (url.protocol !== "https:" && url.protocol !== "http:") return element("p", "Publisher link unavailable.");
  link.href = url.href;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  return link;
}

const views = ["catalog", "staging", "research", "draft", "evaluation", "controls"] as const;
type View = typeof views[number];
let activeView: View = "catalog";
function showView(view: View, focus = true): void {
  activeView = view;
  for (const id of views) node(id).hidden = id !== view;
  for (const link of document.querySelectorAll<HTMLAnchorElement>("#workspace-nav a")) {
    if (link.hash === `#${view}`) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  if (focus) node(`${view}-heading`).focus();
}

function navigate(view: View): void {
  history.pushState(null, "", `#${view}`);
  showView(view);
}

const dialog = node<HTMLDialogElement>("workbench-dialog");
function openDialog(title: string, contents: HTMLElement): void {
  node("dialog-error").textContent = "";
  node("dialog-title").textContent = title;
  node("dialog-content").replaceChildren(contents);
  if (!dialog.open) dialog.showModal();
  node("dialog-title").focus();
}

const pause = (ms: number) => new Promise<void>((resolve) => window.setTimeout(resolve, ms));
function handle(action: () => Promise<void>): () => void {
  return () => void action().catch(showError);
}

let jobs: Ingestion[] = [];
let activeSnapshot: Snapshot | undefined;
async function refreshCatalog(): Promise<void> {
  const [catalog, collection, embedding] = await Promise.all([
    json<{ ingestions: Ingestion[] }>("/catalog"),
    json<{ snapshots: Snapshot[]; active_snapshot_id: string | null }>("/snapshots"),
    json<{ models: string[] }>("/embedding-models"),
  ]);
  jobs = catalog.ingestions;
  activeSnapshot = collection.snapshots.find((snapshot) => snapshot.snapshot_id === collection.active_snapshot_id);
  node("active-collection").textContent = activeSnapshot
    ? `Snapshot ${activeSnapshot.snapshot_id.slice(0, 12)} · ${activeSnapshot.chunk_count} evidence chunks`
    : "No published collection";
  const modes = node<HTMLSelectElement>("retrieval-mode");
  for (const option of modes.options) option.disabled = option.value !== "lexical" && !activeSnapshot?.enabled_modes.includes(option.value as Mode);
  if (modes.selectedOptions[0]?.disabled) modes.value = "lexical";
  node("retrieval-help").textContent = activeSnapshot?.enabled_modes.includes("dense")
    ? "This snapshot supports lexical, dense, and hybrid search."
    : "Dense and hybrid search require a compatible vector snapshot.";
  const sources = node("sources");
  sources.replaceChildren();
  if (!catalog.ingestions.length) {
    sources.append(element("p", "No PDFs have been staged. Use the admission form to add a source."));
  }
  for (const job of jobs) {
    const article = element("article", "", "record");
    const source = job.document;
    article.append(
      element("h3", source.title),
      element("p", `${source.publisher} · ${dateLabel(source)}`, "meta"),
      element("p", source.source_url, "meta"),
      publisherLink(source),
      element("p", `Edition ${source.version_id.slice(0, 12)} · ${job.state.replaceAll("_", " ")}`, "meta"),
      button("Open staged source", handle(() => openStaging(job.job_id)), true),
      button("Withdraw edition", () => showWithdrawal(job), true),
    );
    sources.append(article);
  }
  const snapshots = node("snapshots");
  snapshots.replaceChildren();
  const ready = jobs.filter((job) => job.state === "ready");
  if (ready.length) {
    const form = element("form");
    const model = element("select");
    const lexical = element("option", "Lexical snapshot");
    lexical.value = "";
    model.append(lexical);
    for (const name of embedding.models) {
      const option = element("option", `${name} vector snapshot`);
      option.value = name;
      model.append(option);
    }
    const strategy = element("select");
    for (const [key, title] of [["structure", "Structure-aware"], ["fixed", "Fixed 320 with 40 overlap"]]) {
      const option = element("option", title);
      option.value = key;
      strategy.append(option);
    }
    strategy.disabled = true;
    model.addEventListener("change", () => { strategy.disabled = !model.value; });
    for (const job of ready) {
      const label = element("label", "", "check");
      const checkbox = element("input");
      checkbox.type = "checkbox";
      checkbox.name = "edition";
      checkbox.value = job.job_id;
      checkbox.checked = true;
      label.append(checkbox, document.createTextNode(job.document.title));
      form.append(label);
    }
    form.append(makeLabel("Snapshot configuration", model), makeLabel("Chunking strategy", strategy));
    form.append(button("Prepare snapshot", handle(async () => {
      const settings = model.value ? { embedding_model: model.value, chunking: strategy.value, include_vectors: true } : {};
      announce("Preparing the selected snapshot.");
      await send("/snapshots", { schema_version: "1.1", job_ids: new FormData(form).getAll("edition"), ...settings });
      await refreshCatalog();
      announce("A complete candidate was prepared. Publish it when ready.");
    })));
    snapshots.append(form);
  }
  for (const snapshot of collection.snapshots) {
    const article = element("article", "", "record");
    const isActive = snapshot.snapshot_id === activeSnapshot?.snapshot_id;
    article.append(element("p", `${isActive ? "Active" : snapshot.published_at ? "Previously published" : "Ready to publish"} · ${snapshot.snapshot_id.slice(0, 12)} · ${snapshot.page_count} pages · ${snapshot.enabled_modes.join(", ")}`));
    if (!isActive) {
      const operation = snapshot.published_at ? "restore" : "publish";
      article.append(button(operation === "restore" ? "Restore snapshot" : "Publish snapshot", handle(async () => {
        await send(`/snapshots/${snapshot.snapshot_id}/${operation}`);
        await refreshCatalog();
        announce(operation === "restore" ? "Prior snapshot restored. Current withdrawal rules still apply." : "Snapshot published.");
      })));
    }
    snapshots.append(article);
  }
}

function showWithdrawal(job: Ingestion): void {
  const body = element("div", "", "stack");
  const reason = element("input");
  reason.maxLength = 500;
  const label = element("label", "Reason for withdrawal");
  label.append(reason);
  body.append(element("p", `${job.document.title} will be excluded from new evidence use. Saved drafts keep their mappings with a withdrawal warning.`), label);
  body.append(button("Confirm withdrawal", handle(async () => {
    await send(`/editions/${job.document.version_id}/withdraw`, { schema_version: "1.1", reason: reason.value });
    dialog.close();
    node("search-results").replaceChildren(element("p", "Eligibility changed. Run a new search."));
    announce("Edition withdrawn.");
  })));
  openDialog("Withdraw this edition?", body);
}

let stagedJob: string | undefined;
async function openStaging(id: string, focus = true): Promise<void> {
  stagedJob = id;
  if (focus) navigate("staging");
  const job = await json<Ingestion>(`/ingestions/${id}`);
  if (activeView !== "staging" || stagedJob !== id) return;
  const content = node("staging-content");
  const info = element("article", "", "panel");
  info.append(element("h2", job.document.title), element("p", `${dateLabel(job.document)} · ${job.state.replaceAll("_", " ")}`, "meta"));
  if (job.failure_code) info.append(element("p", `Extraction did not complete (${job.failure_code}). The prior collection remains available.`, "warning"));
  if (["staged", "failed", "canceled", "ready", "review_required"].includes(job.state)) {
    const reviewed = ["ready", "review_required"].includes(job.state);
    const label = reviewed ? "Check extraction configuration" : job.state === "staged" ? "Extract PDF" : "Retry extraction";
    info.append(button(label, handle(async () => {
      const result = await send<{ state: string }>(`/ingestions/${id}/extract`);
      announce(result.state === "extracting"
        ? "Extracting the selected PDF with the bounded worker."
        : "The extraction configuration is unchanged. The saved review remains available.");
      await openStaging(id, false);
    })));
  }
  if (["extracting", "cancel_requested"].includes(job.state)) {
    info.append(button("Cancel extraction", handle(async () => {
      await send(`/ingestions/${id}/cancel`);
      announce("Cancellation requested. Waiting for the worker to stop.");
    }), true));
    window.setTimeout(() => {
      if (activeView === "staging" && stagedJob === id) void openStaging(id, false).catch(showError);
    }, 500);
  }
  content.replaceChildren(info);
  if (job.extraction) showReview(content, job, job.extraction);
}

function makeLabel(label: string, control: HTMLElement): HTMLLabelElement {
  const result = element("label", label);
  result.append(control);
  return result;
}

function showReview(content: HTMLElement, job: Ingestion, data: Extraction): void {
  const panel = element("div", "", "panel stack");
  const selected = new Set(data.review?.approved_span_ids ?? []);
  const normalized = new Set(Object.keys(data.review?.normalizations ?? {}));
  const printedLabels = { ...data.review?.printed_page_labels };
  const allSpans = data.pages.flatMap((page) => page.spans);
  const select = element("select");
  for (const page of data.pages) {
    const option = element("option", `PDF page ${page.pdf_page_1based}`);
    option.value = String(page.pdf_page_1based);
    select.append(option);
  }
  const pageContent = element("div");
  const renderPage = () => {
    const page = data.pages.find((item) => item.pdf_page_1based === Number(select.value));
    if (!page) return;
    const spans = element("ul", "", "source-span-list");
    const printedLabel = element("input");
    const pageKey = String(page.pdf_page_1based);
    printedLabel.value = printedLabels[pageKey] ?? "";
    printedLabel.maxLength = 500;
    printedLabel.disabled = !page.within_use_scope;
    printedLabel.addEventListener("input", () => {
      const value = printedLabel.value.trim();
      if (value) printedLabels[pageKey] = value;
      else delete printedLabels[pageKey];
    });
    pageContent.replaceChildren(
      element("p", page.within_use_scope ? "Compare each selected span against the original page." : "This page is outside the recorded use disposition."),
      button("View original page", handle(async () => {
        const response = await request(`/ingestions/${job.job_id}/pages/${page.pdf_page_1based}`);
        const image = element("img");
        image.alt = `Original PDF page ${page.pdf_page_1based}`;
        image.src = URL.createObjectURL(await response.blob());
        image.addEventListener("load", () => URL.revokeObjectURL(image.src), { once: true });
        const frame = element("div", "", "page-image");
        frame.append(image);
        const scroll = element("div", "", "page-preview");
        scroll.append(frame);
        openDialog("Original page", scroll);
      }), true),
      element("p", `PDF navigation label: ${page.printed_page_label ?? "None"}.`),
      makeLabel("Verified printed page label (optional)", printedLabel),
    );
    if (page.quarantined_lines) pageContent.append(element("p", `${page.quarantined_lines} lines could not be mapped and remain quarantined.`, "warning"));
    for (const span of page.spans) {
      const checkbox = element("input");
      checkbox.type = "checkbox";
      checkbox.disabled = !page.within_use_scope || !!span.excluded_reason;
      checkbox.checked = selected.has(span.span_id);
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) selected.add(span.span_id);
        else selected.delete(span.span_id);
      });
      const label = element("label", "", "check");
      const number = allSpans.indexOf(span) + 1;
      const table = span.table_candidate ? `Table ${span.table_candidate}, row ${span.candidate_row}, column ${span.candidate_column}. ` : "";
      const disposition = span.excluded_reason ? ` (excluded: ${span.excluded_reason})` : span.requires_review ? " (review with table context or quarantine)" : "";
      label.append(checkbox, document.createTextNode(`${number}. ${table}${span.normalized_text}${disposition}`));
      const item = element("li");
      item.append(label);
      if (span.table_candidate && /\d(?:\s+\.|\.[.\s]+)[.\s]*\d/.test(span.normalized_text)) {
        const normalize = element("input");
        normalize.type = "checkbox";
        normalize.checked = normalized.has(span.span_id);
        normalize.addEventListener("change", () => {
          if (normalize.checked) normalized.add(span.span_id);
          else normalized.delete(span.span_id);
        });
        const verified = element("label", "", "check");
        verified.append(normalize, document.createTextNode("I verified the decimal spacing against the original cell."));
        item.append(verified);
      }
      spans.append(item);
    }
    pageContent.append(spans);
  };
  select.addEventListener("change", renderPage);
  renderPage();
  const note = element("textarea");
  note.rows = 2;
  note.maxLength = 500;
  note.value = data.review?.qa_note ?? "";
  const rowContainer = element("div", "", "stack");
  const fields = [
    ["cell_span_ids", "Cell spans"], ["row_header_span_ids", "Row header spans"],
    ["column_header_span_ids", "Column header spans"], ["unit_span_ids", "Unit spans"],
    ["mandatory_note_span_ids", "Mandatory note spans"],
  ] as const;
  const addRow = (saved?: TableLinks) => {
    const group = element("fieldset");
    group.append(element("legend", "Table row"));
    for (const [name, label] of fields) {
      const input = element("input");
      input.name = name;
      input.value = saved?.[name].map((id) => allSpans.findIndex((span) => span.span_id === id) + 1).join(", ") ?? "";
      group.append(makeLabel(label, input));
    }
    const reason = element("input");
    reason.name = "notes_absent_reason";
    reason.value = saved?.notes_absent_reason ?? "";
    group.append(makeLabel("Reason if no notes apply", reason), button("Remove table row", () => group.remove(), true));
    rowContainer.append(group);
  };
  for (const row of data.review?.table_rows ?? []) addRow(row);
  panel.append(
    element("h2", "Extraction review"), makeLabel("Page to review", select), pageContent,
    element("h3", "Table context"),
    element("p", "For a table row, select all supporting spans above and enter their numbers in the relevant fields. Separate multiple numbers with commas."),
    rowContainer, button("Add table row", () => addRow(), true), makeLabel("Original-page comparison notes", note),
    button("Save extraction review", handle(async () => {
      const table_rows = [...rowContainer.querySelectorAll("fieldset")].map((group) => {
        const row = {} as TableLinks;
        for (const [name] of fields) {
          const value = group.querySelector<HTMLInputElement>(`[name="${name}"]`)!.value.trim();
          row[name] = value ? value.split(",").map((number) => {
            const span = allSpans[Number(number.trim()) - 1];
            if (!span) throw new Error("Enter valid span numbers for each table association.");
            return span.span_id;
          }) : [];
        }
        row.notes_absent_reason = group.querySelector<HTMLInputElement>('[name="notes_absent_reason"]')!.value.trim() || null;
        return row;
      });
      await send(`/ingestions/${job.job_id}/review`, {
        schema_version: "1.1", extraction_config_hash: data.config_hash,
        approved_span_ids: [...selected], table_rows, normalized_cell_ids: [...normalized],
        printed_page_labels: printedLabels, qa_note: note.value,
      });
      announce("Extraction review saved. This edition is available for a snapshot.");
      await openStaging(job.job_id, false);
    })),
  );
  content.append(panel);
}

const admission = node<HTMLFormElement>("admission");
admission.addEventListener("submit", async (event) => {
  event.preventDefault();
  const action = admission.querySelector<HTMLButtonElement>('button[type="submit"]')!;
  const values = new FormData(admission);
  const form = new FormData();
  const get = (field: string) => String(values.get(field) ?? "").trim();
  const file = values.get("pdf");
  if (!(file instanceof File) || file.size === 0) {
    announce("", "Choose a local PDF.");
    return;
  }
  form.set("pdf", file);
  form.set("idempotency_key", crypto.randomUUID());
  form.set("manifest", JSON.stringify({
    schema_version: "1.1",
    title: get("title"),
    publisher: get("publisher"),
    source_url: get("source_url"),
    publication_date: get("publication_date") || null,
    unknown_reason: get("unknown_reason") || null,
    scope: get("scope"),
    use_basis: get("use_basis"),
    public_document: values.has("public_document"),
    coverage_regions: [{
      first_page: Number(get("first_page")),
      last_page: Number(get("last_page")),
      use_basis: get("use_basis"),
    }],
  }));
  action.disabled = true;
  announce("Staging the selected PDF.");
  try {
    await json("/ingestions", { method: "POST", body: form });
    admission.reset();
    await refreshCatalog();
    announce("PDF staged. Its original bytes and provenance have been saved.");
  } catch (error) {
    showError(error);
  } finally {
    action.disabled = false;
  }
});

let pendingQuery: string | undefined;
let lastRun: Run | undefined;
const searchForm = node<HTMLFormElement>("search-form");
searchForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const action = searchForm.querySelector<HTMLButtonElement>('button[type="submit"]')!;
  const cancel = node<HTMLButtonElement>("cancel-query");
  action.disabled = true;
  announce("Searching the published collection.");
  try {
    node("generated-answer").replaceChildren();
    node<HTMLButtonElement>("hosted-review").disabled = true;
    const values = new FormData(searchForm);
    const started = await send<{ run_id: string }>("/queries", {
      schema_version: "1.1", question: values.get("question"), context: values.get("context"), mode: values.get("mode"),
    });
    pendingQuery = started.run_id;
    cancel.disabled = false;
    let result: Run;
    do {
      result = await json<Run>(`/queries/${started.run_id}`);
      if (result.status === "pending") await pause(125);
    } while (result.status === "pending");
    lastRun = result;
    node<HTMLButtonElement>("hosted-review").disabled = result.status !== "result";
    renderResults(result);
    node("run-info").replaceChildren(
      element("p", `Run ${result.run_id} · ${result.status}`),
      element("p", `Snapshot ${result.snapshot_id} · eligibility revision ${result.eligibility_revision}`),
      element("pre", JSON.stringify(result.attempts, null, 2)),
    );
  } catch (error) {
    showError(error);
  } finally {
    pendingQuery = undefined;
    cancel.disabled = true;
    action.disabled = false;
  }
});

node("cancel-query").addEventListener("click", handle(async () => {
  if (pendingQuery) await send(`/queries/${pendingQuery}/cancel`);
}));

function renderResults(run: Run): void {
  const list = node("search-results");
  list.replaceChildren();
  if (run.status === "error") {
    announce("", run.failure_code === "embedding_input_too_long" ? "Shorten the question for this model or choose lexical search." : run.failure_code === "embedding_unavailable"
      ? "This retrieval mode is unavailable. Choose lexical search and try again."
      : `Search failed (${run.failure_code ?? "local dependency"}). Your draft is still available.`);
    list.append(element("p", "Search failed. No answer was produced."));
    list.append(button("Use lexical search", () => {
      node<HTMLSelectElement>("retrieval-mode").value = "lexical";
      searchForm.requestSubmit();
    }));
    return;
  }
  if (run.status === "canceled") {
    announce("Search canceled. Your saved draft is still available.");
    return;
  }
  const coverage = run.incomplete_coverage ? " Evidence coverage is incomplete." : "";
  announce(`${run.bundles.length} eligible results · ${run.mode_used ?? "local"} search.${coverage}`);
  if (run.incomplete_coverage) list.append(element("p", "Evidence coverage is incomplete. Unverifiable evidence was omitted.", "warning"));
  if (!run.bundles.length) {
    list.append(element("p", "No eligible evidence matched this question. Revise the search or draft manually."));
    return;
  }
  for (const bundle of run.bundles) {
    const item = element("article", "", "record");
    item.append(
      element("p", bundle.rendered_context),
      button("Inspect evidence", handle(() => inspect(run.run_id, bundle.bundle_id)), true),
    );
    list.append(item);
  }
}

async function inspect(runId: string, bundleId: string, quote?: Quote | null): Promise<void> {
  const path = `/runs/${runId}/evidence/${bundleId}`;
  const bundle = await json<Bundle>(path);
  const doc = bundle.document!;
  const contents = element("div", "", "stack");
  const figure = element("div");
  const region = element("h3");
  region.tabIndex = -1;
  let renderVersion = 0;
  async function selectSpan(span: Span, focus: boolean, selectedQuote?: Quote | null): Promise<void> {
    const version = ++renderVersion;
    const label = span.printed_page_label ? ` · printed page ${span.printed_page_label}` : "";
    region.textContent = `PDF page ${span.pdf_page_1based}${label}`;
    const response = await request(`${path}/pages/${span.span_id}`);
    if (version !== renderVersion) return;
    const frame = element("div", "", "page-image");
    const image = element("img");
    const text = selectedQuote?.text ?? span.normalized_text;
    image.alt = `Original PDF page ${span.pdf_page_1based}. Highlighted region: ${text}`;
    image.src = URL.createObjectURL(await response.blob());
    image.addEventListener("load", () => URL.revokeObjectURL(image.src), { once: true });
    frame.append(image);
    for (const [left, top, right, bottom] of selectedQuote?.bounding_boxes ?? span.bounding_boxes) {
      const box = element("span", "", "highlight");
      box.setAttribute("aria-hidden", "true");
      box.style.left = `${100 * left / span.page_width}%`;
      box.style.top = `${100 * top / span.page_height}%`;
      box.style.width = `${100 * (right - left) / span.page_width}%`;
      box.style.height = `${100 * (bottom - top) / span.page_height}%`;
      frame.append(box);
    }
    const scroll = element("div", "", "page-preview");
    scroll.append(frame);
    figure.replaceChildren(element("p", text), scroll);
    if (focus) region.focus();
  }
  contents.append(element("p", `${doc.publisher} · ${dateLabel(doc)} · edition ${doc.version_id.slice(0, 12)}`, "meta"));
  contents.append(publisherLink(doc));
  if (!bundle.eligible_for_new_use) contents.append(element("p", "Historical evidence. This source is withdrawn and cannot support new claims.", "warning"));
  const links = element("div", "", "actions");
  for (const [index, span] of bundle.spans.entries()) {
    const qualifier = bundle.mandatory_qualifier_span_ids.includes(span.span_id);
    links.append(button(
      qualifier ? `Qualifier ${index + 1}: ${span.normalized_text}` : `Region ${index + 1}`,
      handle(() => selectSpan(span, true)), true,
    ));
  }
  contents.append(links, region, figure);
  if (bundle.eligible_for_new_use) contents.append(button("Add qualified note to draft", handle(async () => {
    await saveDraft({ attach: { run_id: runId, bundle_id: bundleId } });
    dialog.close();
    navigate("draft");
  })));
  openDialog(doc.title, contents);
  const start = bundle.spans.find((span) => span.span_id === quote?.source_span_id) ?? bundle.spans[0];
  await selectSpan(start, false, quote);
}

const draftText = node<HTMLTextAreaElement>("draft-text");
let openedDraft: Draft | undefined;
function draftReference(): { brief_id?: string; parent_revision_id?: string } {
  return openedDraft ? { brief_id: openedDraft.brief_id, parent_revision_id: openedDraft.revision_id } : {};
}
async function saveDraft(options: Record<string, unknown> = {}): Promise<void> {
  const text = "restore_revision_id" in options ? {} : { text: draftText.value };
  const draft = await send<Draft>("/briefs", { schema_version: "1.1", ...draftReference(), ...text, ...options });
  await openDraft(draft);
  await refreshDrafts();
  announce("Draft revision saved.");
}

async function openDraft(draft?: Draft): Promise<void> {
  openedDraft = draft;
  draftText.value = draft?.text ?? "";
  const mappings = node("draft-mappings");
  mappings.replaceChildren();
  const picker = node<HTMLSelectElement>("draft-revision");
  picker.replaceChildren();
  node<HTMLButtonElement>("prepare-export").disabled = !draft;
  node<HTMLButtonElement>("undo-draft").disabled = !draft?.parent_revision_id;
  if (!draft) return;
  for (const claim of draft.claims) {
    const item = element("article", "", "record");
    item.append(element("p", claim.text), element("p", claim.review_status.replaceAll("_", " ").toLowerCase(), "warning"));
    for (const citation of claim.citations) {
      item.append(button(`Inspect ${citation.source_title}`, handle(() => inspect(citation.run_id, citation.bundle_id, citation.quote)), true));
    }
    item.append(button("Detach this evidence association", handle(() => saveDraft({ detach_claim_ids: [claim.claim_id] })), true));
    mappings.append(item);
  }
  const history = await json<{ revisions: Draft[] }>(`/briefs/${draft.brief_id}/revisions`);
  for (const revision of history.revisions) {
    const option = element("option", `${revision.revision_id.slice(0, 12)}${revision.revision_id === draft.revision_id ? " (current)" : ""}`);
    option.value = revision.revision_id;
    picker.append(option);
  }
}

async function refreshDrafts(): Promise<void> {
  const { briefs } = await json<{ briefs: Draft[] }>("/briefs");
  const saved = node("saved-drafts");
  saved.replaceChildren();
  if (!briefs.length) saved.append(element("p", "No saved drafts."));
  for (const draft of briefs) {
    const item = element("article", "", "record");
    item.append(element("p", draft.text.replace(/\[\[EVIDENCE:[a-f0-9]+\]\]/g, "").trim().slice(0, 120)));
    item.append(button("Open saved draft", handle(async () => {
      if (draftText.value !== (openedDraft?.text ?? "") && !window.confirm("Discard unsaved draft edits?")) return;
      await openDraft(draft);
      draftText.focus();
    }), true));
    saved.append(item);
  }
}

node("save-draft").addEventListener("click", handle(() => saveDraft()));
node("new-draft").addEventListener("click", handle(async () => {
  if (draftText.value !== (openedDraft?.text ?? "") && !window.confirm("Discard unsaved draft edits?")) return;
  await openDraft();
  draftText.focus();
}));
node("undo-draft").addEventListener("click", handle(async () => {
  if (draftText.value !== (openedDraft?.text ?? "")) {
    await openDraft(openedDraft);
    announce("Unsaved edits undone.");
  } else if (openedDraft?.parent_revision_id) {
    await saveDraft({ restore_revision_id: openedDraft.parent_revision_id });
  }
}));

node("prepare-export").addEventListener("click", handle(async () => {
  if (!openedDraft) return;
  const format = node<HTMLSelectElement>("export-format").value;
  const operation = await send<{ operation_id: string; revision_id: string }>(`/briefs/${openedDraft.brief_id}/exports`, {
    schema_version: "1.1", format, revision_id: node<HTMLSelectElement>("draft-revision").value,
  });
  const path = `/exports/${operation.operation_id}`;
  const contents = element("div", "", "stack");
  contents.append(element("p", `Saved draft revision ${operation.revision_id.slice(0, 12)} is ready to download.`));
  const download = button("Download saved draft", handle(async () => {
    download.disabled = true;
    try {
      const response = await request(path);
      const link = element("a");
      link.href = URL.createObjectURL(await response.blob());
      link.download = `retrofit-draft-${operation.revision_id.slice(0, 12)}.${format === "json" ? "json" : "md"}`;
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(link.href), 1000);
      dialog.close();
      announce("Saved draft delivered for download.");
    } catch (error) {
      dialog.close();
      showError(error);
    }
  }));
  contents.append(download, button("Cancel pending download", handle(async () => {
    const result = await send<{ state: string }>(`${path}/cancel`);
    dialog.close();
    announce(result.state === "canceled" ? "Download canceled. The saved revision remains available." : "Delivery was already completed.");
  }), true));
  openDialog("Confirm draft download", contents);
  dialog.addEventListener("close", () => {
    void send(`${path}/cancel`).catch(() => {});
  }, { once: true });
}));

node("preview-cleanup").addEventListener("click", handle(async () => {
  const data = await json<{ preview_hash: string; snapshots: { snapshot_id: string }[]; unreferenced_originals: string[] }>("/cleanup");
  const contents = element("div", "", "stack");
  contents.append(element("p", `${data.snapshots.length} unpublished snapshots and ${data.unreferenced_originals.length} unreferenced originals can be removed.`));
  const records = element("ul");
  for (const item of [...data.snapshots.map((snapshot) => snapshot.snapshot_id), ...data.unreferenced_originals]) {
    records.append(element("li", item));
  }
  contents.append(records, element("p", "Published snapshots and retained draft evidence will be preserved."));
  if (records.childElementCount) contents.append(button("Remove listed artifacts", handle(async () => {
    await send("/cleanup", { schema_version: "1.1", preview_hash: data.preview_hash, confirm: true });
    dialog.close();
    await refreshCatalog();
    announce("Local cleanup completed.");
  })));
  openDialog("Local cleanup preview", contents);
}));

node("back-to-catalog").addEventListener("click", handle(async () => {
  navigate("catalog");
  await refreshCatalog();
}));
function routeFromHash(focus = true): void {
  const hash = window.location.hash.slice(1);
  if (hash === "main") return;
  const view = views.find((item) => item === hash && (item !== "staging" || !!stagedJob)) ?? "catalog";
  showView(view, focus);
  if (view === "catalog") void refreshCatalog().catch(showError);
  if (view === "draft") void refreshDrafts().catch(showError);
}
window.addEventListener("hashchange", () => routeFromHash());
routeFromHash(false);
void refreshCatalog().catch(showError);
void refreshDrafts().catch(showError);
type ParserProfile = { enabled: boolean; reason?: string; cpu_seconds: number; memory_bytes: number; wall_seconds: number; network_denied: boolean };
function renderParserProfile(profile: ParserProfile): void {
  node("parser-info").textContent = profile.enabled
    ? `Network denied · ${profile.cpu_seconds}s CPU · ${profile.wall_seconds}s wall time · ${Math.round(profile.memory_bytes / 1048576)} MiB memory`
    : profile.reason ?? "Parser isolation is unavailable.";
}
void json<ParserProfile>("/parser").then(renderParserProfile).catch(showError);

type GenerationState = {
  enabled: boolean; configured: boolean; dispatch_count: number; actual_and_reserved_nanos: number; price_version: string | null;
  budget_holds: string[];
  unreconciled_attempts: { operation_id: string; attempt: number; reserved_nanos: number }[];
};
type Generated = {
  state: string; failure_code?: string; incomplete_coverage: boolean;
  answer?: { status: string; conflicts: string[]; missing_information: string[]; claims: { text: string; qualifiers: string[]; citations: { bundle_id: string; quote?: Quote | null }[] }[] };
};
function generationFailure(result: Generated): string {
  return result.state === "canceled"
    ? "Canceled. Manual work is still available."
    : `Generation failed (${result.failure_code ?? "provider error"}). Use local evidence or draft manually.`;
}

async function refreshGeneration(): Promise<void> {
  const state = await json<GenerationState>("/generation");
  node("generation-note").textContent = state.enabled
    ? "Generation requires confirmation of the exact question and context."
    : "Generation is disabled. Search and manual drafts are available.";
  const info = node("generation-info");
  info.replaceChildren(element("p", `${state.enabled ? "Enabled" : "Disabled"} · ${state.dispatch_count} recorded attempts`));
  info.append(element("p", `Recorded costs and unresolved reservations: $${(state.actual_and_reserved_nanos / 1e9).toFixed(6)}.`, "meta"));
  if (state.price_version) info.append(element("p", state.price_version, "meta"));
  if (state.configured) info.append(button(state.enabled ? "Disable generation" : "Enable authorized generation", handle(async () => {
    await send("/generation", { schema_version: "1.1", enabled: !state.enabled });
    await refreshGeneration();
  })));
  else info.append(element("p", "A separate D3 authorization and verified provider configuration are required.", "meta"));
  for (const { operation_id, attempt, reserved_nanos } of state.unreconciled_attempts) {
    const group = element("fieldset");
    const charge = element("input");
    charge.type = "number";
    charge.min = "0";
    charge.step = "1";
    const reference = element("input");
    group.append(
      element("legend", `Operation ${operation_id}, attempt ${attempt}`),
      element("p", `Reservation of ${reserved_nanos} billionths of a dollar.`, "meta"),
      makeLabel("Actual charge in billionths of a dollar", charge),
      makeLabel("Billing reference", reference),
      button("Record reconciled charge", handle(async () => {
        const actual = charge.valueAsNumber;
        if (!Number.isSafeInteger(actual) || actual < 0 || !reference.value.trim()) {
          throw new Error("Enter the exact nonnegative charge and its billing reference.");
        }
        await send(`/generation/${operation_id}/attempts/${attempt}/reconcile`, {
          schema_version: "1.1", actual_nanos: actual, billing_reference: reference.value,
        });
        await refreshGeneration();
        node("generation-heading").focus();
        announce("Charge recorded.");
      }), true),
    );
    info.append(group);
  }
  for (const operation of state.budget_holds) {
    const path = `/generation/${operation}`;
    info.append(element("p", `Provider-budget hold for operation ${operation}.`));
    const report = element("input");
    report.maxLength = 64;
    info.append(
      makeLabel("Corrective evaluation report SHA-256", report),
      button("Resolve budget hold", handle(async () => {
        await send(`${path}/recover-budget`, { schema_version: "1.1", report_sha256: report.value });
        await refreshGeneration();
        node("generation-heading").focus();
        announce("Budget hold resolved. Generation remains disabled.");
      }), true),
    );
  }
}

node("hosted-review").addEventListener("click", handle(async () => {
  if (!lastRun) return;
  const runId = lastRun.run_id;
  const path = `/queries/${runId}`;
  const preview = await json<GenerationState & { question: string; context: string; confirmation_id: string; notice: string }>(`${path}/hosted-input`);
  const exact = () => node<HTMLTextAreaElement>("question").value.trim() === preview.question
    && node<HTMLTextAreaElement>("query-context").value.trim() === preview.context;
  if (!exact()) throw new Error("The question or context changed. Run search again before hosted review.");
  const contents = element("div", "", "stack");
  const checkbox = element("input");
  checkbox.type = "checkbox";
  checkbox.disabled = !preview.enabled;
  const label = element("label", "", "check");
  label.append(checkbox, document.createTextNode("I confirm these exact inputs contain no confidential information, personal data, or private school or district details."));
  contents.append(
    element("h3", "Exact question"), element("pre", preview.question),
    element("h3", "Exact analyst context"), element("pre", preview.context || "(empty)"),
    element("p", preview.notice), label,
  );
  if (!preview.enabled) contents.append(element("p", "Generation is disabled. Close this dialog to continue working locally.", "warning"));
  const confirm = button("Confirm and request draft", handle(async () => {
    if (!checkbox.checked || !exact()) throw new Error("Review and confirm the current exact inputs before dispatch.");
    confirm.disabled = true;
    const started = await send<{ operation_id: string }>(`${path}/generate`, {
      schema_version: "1.1", confirmed: true, confirmation_id: preview.confirmation_id,
      question: preview.question, context: preview.context,
    });
    dialog.close();
    const pending = node("generated-answer");
    const operation = `/generation/${started.operation_id}`;
    pending.replaceChildren(
      element("p", "Waiting for the authorized generation request."),
      button("Cancel generation", handle(async () => { await send(`${operation}/cancel`); }), true),
    );
    let result: Generated;
    do {
      result = await json<Generated>(operation);
      if (result.state === "pending") await pause(200);
    } while (result.state === "pending");
    pending.replaceChildren(element("h2", "Generated draft"));
    if (!result.answer) {
      pending.append(element("p", generationFailure(result), "warning"));
    } else {
      const answer = result.answer;
      pending.append(element("p", `${answer.status} · Verify evidence and semantic support.`, "warning"));
      if (result.incomplete_coverage) pending.append(element("p", "Evidence coverage is incomplete. Review the cited sources before relying on this draft.", "warning"));
      for (const conflict of answer.conflicts) pending.append(element("p", `Conflict: ${conflict}`));
      for (const item of answer.missing_information) pending.append(element("p", `Missing information: ${item}`));
      for (const claim of answer.claims) {
        pending.append(element("p", claim.text));
        if (claim.qualifiers.length) pending.append(element("p", claim.qualifiers.join(" · "), "meta"));
        for (const citation of claim.citations) pending.append(button(
          citation.quote ? `Inspect quotation: ${citation.quote.text}` : "Inspect generated citation",
          handle(() => inspect(runId, citation.bundle_id, citation.quote)), true,
        ));
      }
      if (answer.claims.length) pending.append(button("Add answer to draft", handle(async () => {
        await saveDraft({ generated_operation_id: started.operation_id });
        navigate("draft");
      })));
    }
    announce(`Generation ${result.state}.`);
    await refreshGeneration();
  }));
  confirm.disabled = true;
  checkbox.addEventListener("change", () => { confirm.disabled = !preview.enabled || !checkbox.checked; });
  contents.append(confirm);
  openDialog("Confirm exact hosted input", contents);
}));

type Report = {
  report_sha256: string; evaluation_mode: string; candidate: string; builder_exposure: string | null;
  decisions: { production: string; retrieval: string; generation: string; eligible_scope: string };
  retrieval: { repetition: number; case_count: number; failures: number; thresholds_met: boolean | null; recall: number | null; table_recall: number | null; returned_precision: number | null; ranked_precision: number | null; recall_interval: [number, number] | null }[];
  generation: { repetition: number; thresholds_met: boolean | null; citation_set_accuracy: number | null; answer_completeness: number | null; consequential_errors: number }[];
  limitations: string[];
};
let reports: Report[] = [];
function renderReport(): void {
  const target = node("evaluation-report");
  const report = reports.find((item) => item.report_sha256 === node<HTMLSelectElement>("report-select").value);
  target.replaceChildren();
  if (!report) {
    target.append(element("p", "No evaluation evidence has been imported."));
    return;
  }
  const repetition = Number(node<HTMLSelectElement>("report-repetition").value);
  const data = report.retrieval.find((item) => item.repetition === repetition);
  const generation = report.generation.find((item) => item.repetition === repetition);
  const percent = (value: number | null) => value === null ? "N/A" : `${(100 * value).toFixed(1)}%`;
  target.append(
    element("h2", report.candidate),
    element("p", report.evaluation_mode.replaceAll("_", " "), "meta"),
    element("p", `Scope eligible for owner review: ${report.decisions.eligible_scope.replaceAll("_", " ")}.`, "warning"),
    element("p", `Production ${report.decisions.production} · Retrieval ${report.decisions.retrieval} · Generation ${report.decisions.generation}.`),
  );
  if (report.builder_exposure) target.append(element("p", `Builder exposure: ${report.builder_exposure}`));
  if (data) target.append(
    element("h3", `Held-out repetition ${repetition}`),
    element("p", `${data.case_count} cases · ${data.failures} task failures`),
    element("p", `Required evidence recall ${percent(data.recall)} · Table recall ${percent(data.table_recall)}`),
    element("p", `Returned precision ${percent(data.returned_precision)} · Ranked precision ${percent(data.ranked_precision)}`),
    element("p", data.recall_interval ? `95% query-cluster interval ${data.recall_interval.map(percent).join(" to ")}` : "95% query-cluster interval unavailable."),
  );
  if (generation) target.append(element("p", `Citation-set accuracy ${percent(generation.citation_set_accuracy)} · Answer completeness ${percent(generation.answer_completeness)} · ${generation.consequential_errors} consequential errors.`));
  for (const item of report.limitations) target.append(element("p", item, "meta"));
}
async function refreshReports(): Promise<void> {
  reports = (await json<{ reports: Report[] }>("/evaluation/reports")).reports;
  const select = node<HTMLSelectElement>("report-select");
  select.replaceChildren();
  for (const item of reports) {
    const option = element("option", `${item.candidate} · ${item.report_sha256.slice(0, 12)}`);
    option.value = item.report_sha256;
    select.append(option);
  }
  renderReport();
}
node("import-report").addEventListener("click", handle(async () => {
  const file = node<HTMLInputElement>("evaluation-file").files?.[0];
  if (!file) throw new Error("Choose an aggregate evaluation report file.");
  await json("/evaluation/reports", { method: "POST", headers: { "Content-Type": "application/json" }, body: await file.text() });
  await refreshReports();
  announce("Evaluation report imported.");
}));
node("report-select").addEventListener("change", renderReport);
node("report-repetition").addEventListener("change", renderReport);
void refreshGeneration().catch(showError);
void refreshReports().catch(showError);

// Narrow component/transport seams for browser contract checks. The workbench
// uses these same implementations; no alternate renderer or request adapter.
export {
  node, element, button, request, announce, showError, dateLabel, publisherLink,
  showView, openDialog, showReview, renderResults,
  renderParserProfile, generationFailure,
};
