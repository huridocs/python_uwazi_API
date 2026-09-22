import * as pdfjs from "./pdf.mjs";

pdfjs.GlobalWorkerOptions.workerSrc = "/static/pdfjs/pdf.worker.mjs";

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

async function render() {
  // viewer.html is served at /viewer/<filename>; the raw bytes at /pdf/<filename>.
  const filename = decodeURIComponent(window.location.pathname.split("/").pop() || "");
  const url = `/pdf/${encodeURIComponent(filename)}`;
  const loadingTask = pdfjs.getDocument(url);
  const pdf = await loadingTask.promise;
  const wrap = document.getElementById("page-wrap");

  for (let pageNum = 1; pageNum <= pdf.numPages; pageNum++) {
    const page = await pdf.getPage(pageNum);
    const viewport = page.getViewport({ scale: 1.5 });

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
    drawHighlights(pageNum, viewport, pageDiv);
    wrap.appendChild(pageDiv);
  }
}

render().catch((err) => {
  document.getElementById("container").textContent = `Failed to render PDF: ${err.message}`;
});
