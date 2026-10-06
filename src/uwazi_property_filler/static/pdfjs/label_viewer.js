// Custom pdf.js viewer for text labeling.
//
// Renders each page (canvas) plus an invisible pdf.js text layer used for
// word-level hit-testing. Clicking a word then another word selects the span
// between them (single-column layout) and posts the selected text items to the
// parent app via `postMessage`. Existing labels/predictions are drawn as
// colored rectangles from the URL hash.

import * as pdfjs from "./pdf.mjs";

pdfjs.GlobalWorkerOptions.workerSrc = "/static/pdfjs/pdf.worker.mjs";

const SCALE = 1.5;

let first = null; // { pageNum, span, index, items }

function readLabels() {
  const hash = decodeURIComponent(window.location.hash || "").replace(/^#/, "");
  if (!hash) return [];
  try {
    return JSON.parse(hash);
  } catch {
    return [];
  }
}

function textSpans(container) {
  // Leaf text spans (exclude `markedContent` container spans), in reading order.
  return [...container.querySelectorAll("span")].filter(
    (s) => s.children.length === 0 && (s.textContent || "").trim() !== ""
  );
}

function spanWord(span, viewport, pageNum) {
  const pageRect = span.closest(".page").getBoundingClientRect();
  const r = span.getBoundingClientRect();
  const cssLeft = r.left - pageRect.left;
  const cssTop = r.top - pageRect.top;
  const [left, top] = viewport.convertToPdfPoint(cssLeft, cssTop);
  return {
    page: pageNum,
    text: (span.textContent || "").trim(),
    left,
    top,
    width: r.width / viewport.scale,
    height: r.height / viewport.scale,
  };
}

function setSpanClass(span, cls, on) {
  span.classList.toggle(cls, on);
}

function clearSelectionMarkers(container) {
  for (const s of container.querySelectorAll("span.pf-sel-first, span.pf-sel-range")) {
    s.classList.remove("pf-sel-first", "pf-sel-range");
  }
}

function onWordClick(pageNum, viewport, span) {
  const container = span.closest(".text-layer");
  const spans = textSpans(container);
  const index = spans.indexOf(span);
  if (index < 0) return;

  if (!first) {
    clearSelectionMarkers(container);
    setSpanClass(span, "pf-sel-first", true);
    first = { pageNum, span, index, container };
    return;
  }

  if (first.pageNum !== pageNum || first.container !== container) {
    // Single-page selection: restart on the new word.
    clearSelectionMarkers(first.container);
    setSpanClass(span, "pf-sel-first", true);
    first = { pageNum, span, index, container };
    return;
  }

  const lo = Math.min(first.index, index);
  const hi = Math.max(first.index, index);
  const range = spans.slice(lo, hi + 1);
  const items = range.map((s) => spanWord(s, viewport, pageNum));
  const text = items.map((it) => it.text).filter(Boolean).join(" ");

  clearSelectionMarkers(container);
  for (const s of range) setSpanClass(s, "pf-sel-range", true);

  window.parent.postMessage(
    { type: "label-selection", page: pageNum, text, items },
    "*"
  );

  first = null;
}

const SOURCE_COLORS = {};

function sourceColor(source) {
  if (!SOURCE_COLORS[source]) {
    let h = 0;
    for (let i = 0; i < source.length; i++) h = (h * 31 + source.charCodeAt(i)) % 360;
    SOURCE_COLORS[source] = h;
  }
  return SOURCE_COLORS[source];
}

function drawLabel(pageDiv, viewport, label) {
  const page = Number(label.page);
  if (!label || label.page !== page) return;
  const [x1, yTop] = viewport.convertToViewportPoint(label.left, label.top);
  const [x2, yBottom] = viewport.convertToViewportPoint(label.left + label.width, label.top - label.height);
  const rect = document.createElement("div");
  rect.className = "label-rect";
  if (label.source === "manual") {
    rect.dataset.source = "manual";
  } else {
    rect.dataset.source = String(label.source || "");
    const h = sourceColor(rect.dataset.source);
    rect.style.background = `hsla(${h}, 70%, 50%, 0.30)`;
    rect.style.borderColor = `hsla(${h}, 70%, 50%, 0.85)`;
  }
  rect.style.left = `${x1}px`;
  rect.style.top = `${yTop}px`;
  rect.style.width = `${x2 - x1}px`;
  rect.style.height = `${yBottom - yTop}px`;
  const conf = label.confidence != null ? ` (${Math.round(label.confidence * 100)}%)` : "";
  rect.title = `${label.label_title || ""} [${label.source}]${conf}`;
  pageDiv.appendChild(rect);
}

async function renderPage(pdf, pageNum, labels) {
  const page = await pdf.getPage(pageNum);
  const viewport = page.getViewport({ scale: SCALE });

  const pageDiv = document.createElement("div");
  pageDiv.className = "page";
  pageDiv.style.width = `${viewport.width}px`;
  pageDiv.style.height = `${viewport.height}px`;

  const canvas = document.createElement("canvas");
  canvas.className = "pdf-page";
  canvas.width = viewport.width;
  canvas.height = viewport.height;
  pageDiv.appendChild(canvas);

  await page.render({ canvasContext: canvas.getContext("2d"), viewport }).promise;

  const textLayerDiv = document.createElement("div");
  textLayerDiv.className = "text-layer";
  textLayerDiv.style.setProperty("--scale-factor", String(viewport.scale));
  pageDiv.appendChild(textLayerDiv);

  const textContent = await page.getTextContent();
  const textLayer = new pdfjs.TextLayer({
    textContentSource: textContent,
    container: textLayerDiv,
    viewport,
  });
  await textLayer.render();

  for (const label of labels) drawLabel(pageDiv, viewport, label);

  for (const span of textSpans(textLayerDiv)) {
    span.addEventListener("click", (e) => {
      e.stopPropagation();
      onWordClick(pageNum, viewport, span);
    });
  }

  return pageDiv;
}

async function render() {
  // Served at /label-viewer/<filename>; the raw bytes at /pdf/<filename>.
  const filename = decodeURIComponent(window.location.pathname.split("/").pop() || "");
  const url = `/pdf/${encodeURIComponent(filename)}`;
  const labels = readLabels();
  const loadingTask = pdfjs.getDocument(url);
  const pdf = await loadingTask.promise;
  const wrap = document.getElementById("page-wrap");

  for (let pageNum = 1; pageNum <= pdf.numPages; pageNum++) {
    wrap.appendChild(await renderPage(pdf, pageNum, labels));
  }
}

render().catch((err) => {
  document.getElementById("container").textContent = `Failed to render PDF: ${err.message}`;
});
