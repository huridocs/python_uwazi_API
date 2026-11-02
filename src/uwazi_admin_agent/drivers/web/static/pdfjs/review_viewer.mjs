import * as pdfjs from "./pdf.mjs";

pdfjs.GlobalWorkerOptions.workerSrc = "/static/pdfjs/pdf.worker.mjs";

// The scale a fresh document opens at. The zoom controls express their label
// as a percentage OF THIS scale, so "100%" means "the familiar default size".
const DEFAULT_SCALE = 1.4;
const MIN_SCALE = 0.5;
const MAX_SCALE = 5.0;
const ZOOM_STEP = 1.25;

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

let pdf = null;
let scale = DEFAULT_SCALE;
// Each re-render takes a token; a newer zoom invalidates the loop of the one
// still in flight, so a fast double-click cannot append pages twice. The
// in-flight page render is cancelled too, so a zoom does not wait for a stale
// page to finish rasterizing.
let renderToken = 0;
let activeRender = null;

function setZoomLabel() {
  const label = document.getElementById("zoom-label");
  if (label) label.textContent = `${Math.round((scale / DEFAULT_SCALE) * 100)}%`;
  const out = document.getElementById("zoom-out");
  const inButton = document.getElementById("zoom-in");
  if (out) out.disabled = scale <= MIN_SCALE + 1e-6;
  if (inButton) inButton.disabled = scale >= MAX_SCALE - 1e-6;
}

async function renderPages() {
  if (!pdf) return;
  const token = ++renderToken;
  if (activeRender) {
    try {
      activeRender.cancel();
    } catch {
      // The task may already have finished; nothing to cancel.
    }
    activeRender = null;
  }
  const wrap = document.getElementById("page-wrap");
  const container = document.getElementById("container");
  // Keep the reader roughly where they were: remember the relative scroll
  // position, then restore it once the taller/shorter pages are laid out.
  const maxBefore = container.scrollHeight - container.clientHeight;
  const ratio = maxBefore > 0 ? container.scrollTop / maxBefore : 0;

  wrap.innerHTML = "";
  setZoomLabel();

  for (let pageNum = 1; pageNum <= pdf.numPages; pageNum++) {
    const page = await pdf.getPage(pageNum);
    if (token !== renderToken) return;
    const viewport = page.getViewport({ scale });

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

    const task = page.render({ canvasContext: canvas.getContext("2d"), viewport });
    activeRender = task;
    try {
      await task.promise;
    } catch (err) {
      if (token !== renderToken) return; // cancelled by a newer zoom
      throw err;
    }
    if (activeRender === task) activeRender = null;
    if (token !== renderToken) return;
    wrap.appendChild(pageDiv);
  }

  const maxAfter = container.scrollHeight - container.clientHeight;
  if (maxAfter > 0) container.scrollTop = ratio * maxAfter;
}

async function render() {
  const loadingTask = pdfjs.getDocument(documentUrl());
  pdf = await loadingTask.promise;
  await renderPages();
}

function zoomBy(factor) {
  const next = Math.min(MAX_SCALE, Math.max(MIN_SCALE, scale * factor));
  if (Math.abs(next - scale) < 1e-6) return;
  scale = Math.round(next * 100) / 100;
  renderPages().catch(showError);
}

function showError(err) {
  const container = document.getElementById("container");
  container.innerHTML = `<div id="error">No renderable primary document (${err.message}).</div>`;
}

document.getElementById("zoom-in").addEventListener("click", () => zoomBy(ZOOM_STEP));
document.getElementById("zoom-out").addEventListener("click", () => zoomBy(1 / ZOOM_STEP));

render().catch(showError);
