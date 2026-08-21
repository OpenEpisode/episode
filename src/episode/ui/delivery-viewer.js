import { API } from "./api.js?v=3";
import { escHtml } from "./dom.js";
import { fmt, titleCase } from "./format.js?v=3";

const PREVIEW_LIMIT = 1024 * 1024;

let deliveries = [];
let activeIndex = 0;
let requestVersion = 0;
let objectUrl = "";

function hexDump(buffer, limit = 4096) {
  const bytes = new Uint8Array(buffer);
  const shown = bytes.subarray(0, Math.min(bytes.length, limit));
  const lines = [];
  for (let offset = 0; offset < shown.length; offset += 16) {
    const row = shown.subarray(offset, Math.min(offset + 16, shown.length));
    const hex = Array.from(row, byte => byte.toString(16).padStart(2, "0"))
      .join(" ")
      .padEnd(47, " ");
    const ascii = Array.from(row, byte =>
      byte >= 32 && byte <= 126 ? String.fromCharCode(byte) : "."
    ).join("");
    lines.push(`${offset.toString(16).padStart(8, "0")}  ${hex}  |${ascii}|`);
  }
  return lines.join("\n");
}

function looksLikeText(buffer) {
  const bytes = new Uint8Array(buffer);
  if (!bytes.length) return true;
  const sample = bytes.subarray(0, Math.min(bytes.length, 8192));
  let printable = 0;
  for (const byte of sample) {
    if (byte === 9 || byte === 10 || byte === 13 || (byte >= 32 && byte < 127)) printable += 1;
  }
  return printable / sample.length > 0.86;
}

export function prettyXml(text) {
  const markerOffsets = [text.indexOf("<?xml"), text.indexOf("<EventNotificationAlert")]
    .filter(offset => offset >= 0);
  const start = markerOffsets.length ? Math.min(...markerOffsets) : 0;
  const prefix = text.slice(0, start).trim();
  const xml = text.slice(start).replace(/>\s*</g, "><").replace(/(>)(<)(\/*)/g, "$1\n$2$3");
  let depth = 0;
  const formatted = xml.split("\n").map(line => {
    const value = line.trim();
    if (/^<\//.test(value)) depth = Math.max(0, depth - 1);
    const result = `${"  ".repeat(depth)}${value}`;
    if (
      /^<[^!?/][^>]*>$/.test(value)
      && !/<\/[^>]+>$/.test(value)
      && !/\/>$/.test(value)
    ) {
      depth += 1;
    }
    return result;
  }).join("\n");
  return prefix ? `${prefix}\n\n${formatted}` : formatted;
}

export function formatTextPayload(text, mimeType = "") {
  const trimmed = text.trim();
  if (mimeType.includes("json") || trimmed.startsWith("{") || trimmed.startsWith("[")) {
    try {
      return { language: "json", text: JSON.stringify(JSON.parse(text), null, 2) };
    } catch { /* Preserve malformed JSON as text. */ }
  }
  if (mimeType.includes("xml") || trimmed.includes("<?xml") || /^<[^>]+>/.test(trimmed)) {
    return { language: "xml", text: prettyXml(text) };
  }
  return { language: "text", text };
}

function clearObjectUrl() {
  if (!objectUrl) return;
  URL.revokeObjectURL(objectUrl);
  objectUrl = "";
}

function ensureViewer() {
  let viewer = document.getElementById("delivery-viewer");
  if (viewer) return viewer;
  viewer = document.createElement("div");
  viewer.id = "delivery-viewer";
  viewer.className = "delivery-viewer hidden";
  viewer.innerHTML = `
    <div class="delivery-viewer-backdrop" data-delivery-close></div>
    <section class="delivery-viewer-dialog" role="dialog" aria-modal="true" aria-labelledby="delivery-viewer-title">
      <header class="delivery-viewer-header">
        <div>
          <div class="eyebrow">Immutable source</div>
          <h2 id="delivery-viewer-title">Original delivery</h2>
          <div id="delivery-viewer-subtitle" class="meta"></div>
        </div>
        <button class="icon-button" type="button" data-delivery-close aria-label="Close">×</button>
      </header>
      <div class="delivery-viewer-toolbar">
        <button id="delivery-viewer-prev" class="button button-ghost" type="button">← Previous</button>
        <span id="delivery-viewer-counter" class="pagination-summary"></span>
        <button id="delivery-viewer-next" class="button button-ghost" type="button">Next →</button>
      </div>
      <div id="delivery-viewer-content" class="delivery-viewer-content"></div>
      <footer class="delivery-viewer-footer">
        <div id="delivery-viewer-facts" class="delivery-viewer-facts"></div>
        <a id="delivery-viewer-download" class="button button-primary" download>Download original</a>
      </footer>
    </section>`;
  document.body.appendChild(viewer);
  viewer.querySelectorAll("[data-delivery-close]").forEach(element => {
    element.addEventListener("click", closeDeliveryViewer);
  });
  viewer.querySelector("#delivery-viewer-prev").addEventListener("click", () => {
    deliveryViewerNav(-1);
  });
  viewer.querySelector("#delivery-viewer-next").addEventListener("click", () => {
    deliveryViewerNav(1);
  });
  return viewer;
}

async function renderDelivery() {
  const viewer = ensureViewer();
  const delivery = deliveries[activeIndex];
  if (!delivery) return;
  const version = ++requestVersion;
  clearObjectUrl();

  const content = viewer.querySelector("#delivery-viewer-content");
  content.innerHTML = '<div class="delivery-viewer-loading">Loading preserved delivery…</div>';
  viewer.querySelector("#delivery-viewer-title").textContent = delivery.source || "Original delivery";
  viewer.querySelector("#delivery-viewer-subtitle").textContent =
    `${titleCase(delivery.transport || "unknown")} transport · ${fmt(delivery.received_at)}`;
  viewer.querySelector("#delivery-viewer-counter").textContent =
    `${activeIndex + 1} of ${deliveries.length}`;
  viewer.querySelector("#delivery-viewer-prev").disabled = deliveries.length < 2;
  viewer.querySelector("#delivery-viewer-next").disabled = deliveries.length < 2;

  const artifactUrl = delivery.artifact_url
    || `${API}/receipts/${encodeURIComponent(delivery.id)}/artifact`;
  const download = viewer.querySelector("#delivery-viewer-download");
  download.href = artifactUrl;
  viewer.querySelector("#delivery-viewer-facts").innerHTML = `
    <span><strong>Status</strong>${escHtml(delivery.status)}</span>
    <span><strong>Transport</strong>${escHtml(titleCase(delivery.transport || "unknown"))}</span>
    <span><strong>Receipt</strong><code>${escHtml(delivery.id)}</code></span>`;

  try {
    const response = await fetch(artifactUrl);
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    const blob = await response.blob();
    if (version !== requestVersion) return;
    const mimeType = (blob.type || "application/octet-stream").toLowerCase();

    if (mimeType.startsWith("image/")) {
      objectUrl = URL.createObjectURL(blob);
      content.innerHTML = `<img class="delivery-media" src="${objectUrl}" alt="Original delivery preview">`;
      return;
    }
    if (mimeType.startsWith("video/")) {
      objectUrl = URL.createObjectURL(blob);
      content.innerHTML = `<video class="delivery-media" src="${objectUrl}" controls></video>`;
      return;
    }
    if (mimeType.startsWith("audio/")) {
      objectUrl = URL.createObjectURL(blob);
      content.innerHTML = `<audio class="delivery-audio" src="${objectUrl}" controls></audio>`;
      return;
    }

    const previewBlob = blob.slice(0, PREVIEW_LIMIT);
    const buffer = await previewBlob.arrayBuffer();
    if (version !== requestVersion) return;
    const truncated = blob.size > previewBlob.size;
    const textual = mimeType.startsWith("text/")
      || mimeType.includes("json")
      || mimeType.includes("xml")
      || looksLikeText(buffer);
    if (textual) {
      const decoded = new TextDecoder("utf-8", { fatal: false }).decode(buffer);
      const formatted = formatTextPayload(decoded, mimeType);
      content.innerHTML = `
        <div class="delivery-preview-note">Formatted ${formatted.language.toUpperCase()} preview · original bytes remain unchanged${truncated ? " · preview truncated" : ""}</div>
        <pre class="payload-xml delivery-code"><code>${escHtml(formatted.text)}</code></pre>`;
      return;
    }

    content.innerHTML = `
      <div class="delivery-preview-note">Binary preview · ${Math.min(buffer.byteLength, 4096)} of ${blob.size} bytes shown</div>
      <pre class="payload-xml payload-binary delivery-code"><code>${escHtml(hexDump(buffer))}</code></pre>`;
  } catch (error) {
    if (version !== requestVersion) return;
    content.innerHTML = `<div class="notice notice-warning"><strong>Preview unavailable</strong><span>${escHtml(error.message)}</span></div>`;
  }
}

export function openDeliveryViewer(items, index = 0) {
  deliveries = (items || []).filter(delivery => delivery.has_artifact);
  if (!deliveries.length) return;
  activeIndex = Math.min(Math.max(index, 0), deliveries.length - 1);
  const viewer = ensureViewer();
  viewer.classList.remove("hidden");
  document.body.classList.add("dialog-open");
  renderDelivery();
}

export function closeDeliveryViewer() {
  const viewer = document.getElementById("delivery-viewer");
  if (!viewer || viewer.classList.contains("hidden")) return;
  requestVersion += 1;
  clearObjectUrl();
  viewer.classList.add("hidden");
  viewer.querySelectorAll("video, audio").forEach(media => media.pause());
  document.body.classList.remove("dialog-open");
}

export function deliveryViewerNav(delta) {
  if (deliveries.length < 2) return;
  activeIndex = (activeIndex + delta + deliveries.length) % deliveries.length;
  renderDelivery();
}

if (typeof document !== "undefined") {
  document.addEventListener("keydown", event => {
    const viewer = document.getElementById("delivery-viewer");
    if (!viewer || viewer.classList.contains("hidden")) return;
    if (event.key === "Escape") closeDeliveryViewer();
    if (event.key === "ArrowLeft") deliveryViewerNav(-1);
    if (event.key === "ArrowRight") deliveryViewerNav(1);
  });
}
