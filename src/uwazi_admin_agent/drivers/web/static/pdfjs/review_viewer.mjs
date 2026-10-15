import * as pdfjs from "./pdf.mjs";

pdfjs.GlobalWorkerOptions.workerSrc = "/static/pdfjs/pdf.worker.mjs";

// Two ways to say which document to render:
//  - `?src=<url>` — an explicit document URL (used when the caller already knows
//    the exact route to fetch);
//  - `?run=<run_id>&sid=<shared_id>` — this app's per-entity primary-document
//    route, which is the default.
function documentUrl() {
  const params = new URLSearchParams(window.location.search);
  const explicit = params.get("src");
  if (explicit) return explicit;
  const runId = params.get("run") || "";
  const sharedId = params.get("sid") || "";
  return `/review-pdf?run=${encodeURIComponent(runId)}&sid=${encodeURIComponent(sharedId)}`;
}

async function render() {
  const loadingTask = pdfjs.getDocument(documentUrl());
  const pdf = await loadingTask.promise;
  const wrap = document.getElementById("page-wrap");

  for (let pageNum = 1; pageNum <= pdf.numPages; pageNum++) {
    const page = await pdf.getPage(pageNum);
    const viewport = page.getViewport({ scale: 1.4 });

    const pageDiv = document.createElement("div");
    pageDiv.style.position = "relative";
    pageDiv.style.margin = "12px auto";
    pageDiv.style.width = `${viewport.width}px`;
    pageDiv.style.height = `${viewport.height}px`;

    const canvas = document.createElement("canvas");
    canvas.className = "pdf-page";
    canvas.width = viewport.width;
    canvas.height = viewport.height;
    pageDiv.appendChild(canvas);

    await page.render({ canvasContext: canvas.getContext("2d"), viewport }).promise;
    wrap.appendChild(pageDiv);
  }
}

render().catch((err) => {
  const container = document.getElementById("container");
  container.innerHTML = `<div id="error">No renderable primary document (${err.message}).</div>`;
});
