import * as pdfjs from "./pdf.mjs";

pdfjs.GlobalWorkerOptions.workerSrc = "/static/pdfjs/pdf.worker.mjs";

const DEFAULT_SCALE = 1.5;
const MIN_SCALE = DEFAULT_SCALE * 0.5;
const MAX_SCALE = DEFAULT_SCALE * 3;
const ZOOM_STEP = DEFAULT_SCALE * 0.2;

const wrap = document.getElementById("page-wrap");
const errorBox = document.getElementById("error");
const zoomLevel = document.getElementById("zoom-level");
const zoomInButton = document.getElementById("zoom-in");
const zoomOutButton = document.getElementById("zoom-out");

let pdf = null;
let scale = DEFAULT_SCALE;
let renderToken = 0;

// Highlight JSON: array of {page, left, top, width, height} in PDF user-space.
function readHighlights() {
  const hash = decodeURIComponent(window.location.hash || "").replace(/^#/, "");
  if (!hash) return [];
  try {
    return JSON.parse(hash);
  } catch {
    return [];
  }
}

// PDF user-space coordinates are in points with (0,0) at bottom-left; pdf.js
// viewport transform maps them to CSS pixels with (0,0) at top-left.
function drawHighlights(pageNum, viewport, pageDiv) {
  const highlights = readHighlights().filter((h) => h.page === pageNum);
  for (const h of highlights) {
    const [x1, yTop] = viewport.convertToViewportPoint(h.left, h.top);
    const [x2, yBottom] = viewport.convertToViewportPoint(h.left + h.width, h.top - h.height);
    const rect = document.createElement("div");
    rect.className = "highlight-rect";
    rect.style.left = `${x1}px`;
    rect.style.top = `${yTop}px`;
    rect.style.width = `${x2 - x1}px`;
    rect.style.height = `${yBottom - yTop}px`;
    pageDiv.appendChild(rect);
  }
}

function updateZoomControls() {
  zoomLevel.textContent = `${Math.round((scale / DEFAULT_SCALE) * 100)}%`;
  zoomOutButton.disabled = scale <= MIN_SCALE + 1e-9;
  zoomInButton.disabled = scale >= MAX_SCALE - 1e-9;
}

function showError(err) {
  errorBox.textContent = `Failed to render PDF: ${err.message}`;
  errorBox.style.display = "grid";
}

async function renderPages() {
  if (!pdf) return;
  const token = ++renderToken;
  const currentScale = scale;
  wrap.replaceChildren();

  for (let pageNum = 1; pageNum <= pdf.numPages; pageNum++) {
    if (token !== renderToken) return; // a newer zoom superseded this render
    const page = await pdf.getPage(pageNum);
    const viewport = page.getViewport({ scale: currentScale });

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
    if (token !== renderToken) return;
    drawHighlights(pageNum, viewport, pageDiv);
    wrap.appendChild(pageDiv);
  }
}

function setScale(next) {
  const clamped = Math.min(MAX_SCALE, Math.max(MIN_SCALE, next));
  if (Math.abs(clamped - scale) < 1e-9) return;
  scale = clamped;
  updateZoomControls();
  renderPages().catch(showError);
}

async function render() {
  // viewer.html is served at /viewer/<filename>; the raw bytes at /pdf/<filename>.
  const filename = decodeURIComponent(window.location.pathname.split("/").pop() || "");
  const url = `/pdf/${encodeURIComponent(filename)}`;
  const loadingTask = pdfjs.getDocument(url);
  pdf = await loadingTask.promise;
  updateZoomControls();
  await renderPages();
}

zoomInButton.addEventListener("click", () => setScale(scale + ZOOM_STEP));
zoomOutButton.addEventListener("click", () => setScale(scale - ZOOM_STEP));

render().catch(showError);
