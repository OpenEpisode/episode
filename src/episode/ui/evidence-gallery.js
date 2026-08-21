import { API, api } from "./api.js?v=3";
import { $, escHtml } from "./dom.js";
import { fmtBytes, fmtShort, fmtTime, plural, titleCase, trunc } from "./format.js?v=3";

let carouselItems = [];
let carouselIndex = 0;
let boundingBoxRequest = 0;

export function originBadge(evidence) {
  const origin = evidence.metadata?.origin || evidence.evidence_type;
  const className = origin === "isapi" ? "badge-isapi"
    : origin === "alarm_server" ? "badge-alarm"
    : origin === "ftp" ? "badge-ftp"
    : origin === "recording" ? "badge-recording"
    : "badge-payload";
  return `<span class="badge ${className}">${escHtml(titleCase(origin))}</span>`;
}

function shortDuration(seconds) {
  if (!seconds && seconds !== 0) return "";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = seconds % 60;
  const pad = value => String(value).padStart(2, "0");
  if (hours > 0) return `${hours}:${pad(minutes)}:${pad(remainder)}`;
  if (minutes > 0) return `${minutes}:${pad(remainder)}`;
  return `${remainder}s`;
}

function renderEvidenceItem(evidence, index) {
  const isVideo = evidence.mime_type?.startsWith("video/");
  const isImage = evidence.mime_type?.startsWith("image/");
  const duration = evidence.metadata?.duration_seconds;
  const label = evidence.evidence_type;
  return `<article class="evidence-item" tabindex="0" role="button" onclick="showCarousel(null, ${index})" onkeydown="if(event.key==='Enter'||event.key===' ')showCarousel(null, ${index})">
    <div class="evidence-item-preview">
      ${isVideo ? `<video src="${API}/evidence/${evidence.id}/file" preload="metadata"></video>` : ""}
      ${isImage ? `<img src="${API}/evidence/${evidence.id}/file" loading="lazy" alt="">` : ""}
      ${!isVideo && !isImage ? `<div class="evidence-item-file"><svg><use href="icons.svg?v=2#file"></use></svg><strong>${escHtml(titleCase(evidence.evidence_type))}</strong><span>${escHtml(evidence.mime_type || "Unknown format")}</span></div>` : ""}
      <span class="evidence-type-chip">${escHtml(titleCase(evidence.evidence_type))}</span>
    </div>
    <div class="evidence-item-body">
      <div class="evidence-item-heading">${originBadge(evidence)}<strong>${escHtml(titleCase(label))}</strong></div>
      <div class="evidence-item-context">
        <span title="Device"><svg><use href="icons.svg?v=2#devices"></use></svg>${escHtml(evidence.device_id || "Unknown Device")}</span>
        <span title="Captured"><svg><use href="icons.svg?v=2#clock"></use></svg>${fmtShort(evidence.timestamp)}</span>
        ${duration ? `<span title="Duration"><svg><use href="icons.svg?v=2#clock"></use></svg>${shortDuration(duration)}</span>` : ""}
      </div>
      <a href="#evidence/${evidence.id}" onclick="event.stopPropagation()">Evidence details</a>
    </div>
  </article>`;
}

export function renderEvidenceGrid(list) {
  const items = list.filter(evidence => evidence.evidence_type !== "payload");
  if (!items.length) return '<div class="empty">No evidence</div>';
  carouselItems = items;
  return `<div class="evidence-grid">
    ${items.map((evidence, index) => renderEvidenceItem(evidence, index)).join("")}
  </div>`;
}

function captureRange(group) {
  if (!group.firstCaptureAt) return "Capture time unavailable";
  if (group.firstCaptureAt === group.lastCaptureAt) return fmtShort(group.firstCaptureAt);
  const first = new Date(group.firstCaptureAt);
  const last = new Date(group.lastCaptureAt);
  const sameDay = first.toDateString() === last.toDateString();
  return sameDay
    ? `${fmtShort(group.firstCaptureAt)} → ${fmtTime(group.lastCaptureAt)}`
    : `${fmtShort(group.firstCaptureAt)} → ${fmtShort(group.lastCaptureAt)}`;
}

function renderEvidenceBundle(group, items, deviceNames, areaNames) {
  const first = group.evidence[0];
  const areaName = areaNames.get(first?.area_id) || first?.area_id || "Unknown Area";
  return `<section class="evidence-archive-group ${group.attention ? "needs-attention" : ""}">
    <header class="evidence-archive-heading">
      <div class="evidence-bundle-heading">
        ${group.attention
          ? `<span class="eyebrow">Needs attention</span>
            <h3><svg><use href="icons.svg?v=2#evidence"></use></svg>Unassigned evidence</h3>
            <p>These captured artifacts are not associated with an Episode.</p>`
          : `<h3><svg><use href="icons.svg?v=2#episodes"></use></svg><span><small>Episode</small><code>${escHtml(group.episodeId)}</code></span></h3>`}
        <div class="evidence-bundle-meta">
          ${group.episodeId ? `<span title="Area"><svg><use href="icons.svg?v=2#areas"></use></svg><span><small>Area</small><strong>${escHtml(areaName)}</strong></span></span>` : ""}
          <span title="Bundle contents"><svg><use href="icons.svg?v=2#evidence"></use></svg><span><small>Contents</small><strong>${plural(group.evidence.length, "artifact")}</strong></span></span>
          <span title="Contributing Devices"><svg><use href="icons.svg?v=2#devices"></use></svg><span><small>Devices</small><strong>${plural(group.deviceCount, "Device")}</strong></span></span>
          <span title="Capture period"><svg><use href="icons.svg?v=2#clock"></use></svg><span><small>Captured</small><strong>${captureRange(group)}</strong></span></span>
        </div>
      </div>
      ${group.episodeId
        ? `<a class="button button-ghost" href="#episode/${escHtml(group.episodeId)}">Open Episode</a>`
        : '<span class="badge badge-warning">Not in an Episode</span>'}
    </header>
    <div class="evidence-archive-grid">
      ${group.evidence.map(evidence => {
        const index = items.indexOf(evidence);
        const isVideo = evidence.mime_type?.startsWith("video/");
        const isImage = evidence.mime_type?.startsWith("image/");
        const deviceName = deviceNames.get(evidence.device_id) || evidence.device_id;
        return `<article class="evidence-archive-item" tabindex="0" role="button" onclick="showCarousel(null, ${index})" onkeydown="if(event.key==='Enter')showCarousel(null, ${index})">
          <div class="evidence-archive-preview">
            ${isVideo ? `<video src="${API}/evidence/${evidence.id}/file" preload="metadata"></video>` : ""}
            ${isImage ? `<img src="${API}/evidence/${evidence.id}/file" loading="lazy" alt="">` : ""}
            ${!isVideo && !isImage ? `<div class="evidence-file-preview"><strong>${escHtml(titleCase(evidence.evidence_type))}</strong><span>${escHtml(evidence.mime_type || "Unknown format")}</span></div>` : ""}
            <span class="evidence-type-chip">${escHtml(titleCase(evidence.evidence_type))}</span>
          </div>
          <div class="evidence-archive-body">
            <div class="evidence-device"><svg><use href="icons.svg?v=2#devices"></use></svg><strong>${escHtml(deviceName || "Unknown Device")}</strong></div>
            <div class="evidence-item-meta">
              <span title="Captured"><svg><use href="icons.svg?v=2#clock"></use></svg>${fmtShort(evidence.timestamp)}</span>
              <span title="File size"><svg><use href="icons.svg?v=2#file"></use></svg>${fmtBytes(evidence.byte_size)}</span>
            </div>
            <div class="evidence-archive-links">
              <a href="#evidence/${evidence.id}" onclick="event.stopPropagation()">Details</a>
              ${evidence.event_id ? `<a href="#event/${evidence.event_id}" onclick="event.stopPropagation()">Activity</a>` : ""}
            </div>
          </div>
        </article>`;
      }).join("")}
    </div>
  </section>`;
}

export function renderEvidenceArchive(periods, deviceNames, areaNames) {
  const bundles = periods.flatMap(period => period.bundles);
  const items = bundles.flatMap(bundle => bundle.evidence);
  carouselItems = items;
  return `<div class="evidence-archive">
    ${periods.map(period => `<section class="evidence-period">
      <header class="evidence-period-heading"><strong>${escHtml(period.label)}</strong><span>${plural(period.bundles.length, "Episode bundle")}</span></header>
      <div class="evidence-rail-list">
        ${period.bundles.map(bundle => `<div class="evidence-rail-item ${bundle.attention ? "needs-attention" : ""}">
          <time datetime="${escHtml(bundle.firstCaptureAt || "")}">${fmtTime(bundle.firstCaptureAt)}</time>
          <div class="evidence-rail-track"><span></span></div>
          ${renderEvidenceBundle(bundle, items, deviceNames, areaNames)}
        </div>`).join("")}
      </div>
    </section>`).join("")}
  </div>`;
}

export function renderEpisodeEvidence(list) {
  const sorted = [...list].sort(
    (left, right) => new Date(left.timestamp) - new Date(right.timestamp),
  );
  if (!sorted.length) return '<div class="empty">No evidence</div>';
  carouselItems = sorted;
  const recordings = sorted.filter(evidence => evidence.evidence_type === "recording");
  const snapshots = sorted.filter(evidence => evidence.evidence_type === "snapshot");
  const other = sorted.filter(evidence =>
    evidence.evidence_type !== "recording" && evidence.evidence_type !== "snapshot"
  );
  const section = (title, items) => items.length ? `
    <section class="episode-evidence-group">
      ${title ? `<header><strong>${title}</strong><span>${items.length}</span></header>` : ""}
      <div class="evidence-grid">
        ${items.map(evidence => renderEvidenceItem(evidence, sorted.indexOf(evidence))).join("")}
      </div>
    </section>` : "";
  return section("Recordings", recordings)
    + section("Snapshots", snapshots)
    + section("", other);
}

function stopCurrentMedia() {
  const slide = $("#carousel-slide");
  for (const element of slide.querySelectorAll("video, audio")) {
    element.pause();
    element.removeAttribute("src");
    element.load();
  }
}

function renderCarousel() {
  const evidence = carouselItems[carouselIndex];
  if (!evidence) return;
  stopCurrentMedia();

  const isVideo = evidence.mime_type?.startsWith("video/");
  const isImage = evidence.mime_type?.startsWith("image/");
  let mediaHtml = "";
  if (isVideo) {
    mediaHtml = `<video src="${API}/evidence/${evidence.id}/file" controls autoplay></video>`;
  } else if (isImage) {
    mediaHtml = `<div class="carousel-image-frame">
      <img src="${API}/evidence/${evidence.id}/file" alt="">
      <svg id="carousel-bbox" viewBox="0 0 1 1" preserveAspectRatio="none">
        <rect id="carousel-bbox-rect" x="0" y="0" width="0" height="0" fill="none" stroke="#00C2C7" stroke-width="0.008" stroke-linecap="round">
          <animate attributeName="stroke-opacity" values="1;0.4;1" dur="2s" repeatCount="indefinite"/>
        </rect>
      </svg>
    </div>`;
  } else {
    mediaHtml = `<div class="carousel-file-preview"><svg><use href="icons.svg?v=2#file"></use></svg><strong>${escHtml(titleCase(evidence.evidence_type))}</strong><span>${escHtml(evidence.mime_type || "Unknown format")}</span></div>`;
  }

  $("#carousel-slide").innerHTML = mediaHtml;
  $("#carousel-title").textContent = titleCase(evidence.evidence_type);
  $("#carousel-counter").textContent = `${carouselIndex + 1} of ${carouselItems.length}`;
  $("#carousel-prev").disabled = carouselItems.length < 2;
  $("#carousel-next").disabled = carouselItems.length < 2;
  const info = $("#carousel-info");
  info.innerHTML = `<div class="carousel-evidence-context">
      ${originBadge(evidence)}
      <span><svg><use href="icons.svg?v=2#devices"></use></svg>${escHtml(trunc(evidence.device_id || "Unknown Device", 28))}</span>
      <span><svg><use href="icons.svg?v=2#clock"></use></svg>${fmtShort(evidence.timestamp)}</span>
    </div>
    <nav class="carousel-links">
      <a href="#evidence/${evidence.id}" onclick="closeCarousel()">Evidence details</a>
      ${evidence.event_id ? `<a href="#event/${evidence.event_id}" onclick="closeCarousel()">Activity</a>` : ""}
      ${evidence.episode_id ? `<a href="#episode/${evidence.episode_id}" onclick="closeCarousel()">Episode</a>` : ""}
    </nav>`;

  if (isImage && evidence.episode_id) {
    const requestId = ++boundingBoxRequest;
    api("/evidence/" + evidence.id + "/closest-event")
      .then(closest => {
        if (requestId !== boundingBoxRequest || !closest?.bounding_box) return;
        const rectangle = document.getElementById("carousel-bbox-rect");
        if (!rectangle) return;
        rectangle.setAttribute("x", closest.bounding_box.x);
        rectangle.setAttribute("y", closest.bounding_box.y);
        rectangle.setAttribute("width", closest.bounding_box.width);
        rectangle.setAttribute("height", closest.bounding_box.height);
        document.getElementById("carousel-bbox").classList.add("visible");
        if (closest.target_type) {
          info.querySelector(".carousel-evidence-context")?.insertAdjacentHTML(
            "afterbegin",
            `<span class="badge badge-neutral">${escHtml(titleCase(closest.target_type))}</span>`,
          );
        }
      })
      .catch(() => {});
  }
}

export function showCarousel(items, index) {
  if (Array.isArray(items)) {
    carouselItems = items.filter(evidence => evidence.evidence_type !== "payload");
  }
  carouselIndex = index;
  renderCarousel();
  $("#evidence-carousel").classList.remove("hidden");
  document.body.style.overflow = "hidden";
}

export function closeCarousel() {
  $("#evidence-carousel").classList.add("hidden");
  document.body.style.overflow = "";
  boundingBoxRequest += 1;
  stopCurrentMedia();
}

export function carouselNav(delta) {
  if (!carouselItems.length) return;
  carouselIndex = (carouselIndex + delta + carouselItems.length) % carouselItems.length;
  renderCarousel();
}

window.showCarousel = showCarousel;
window.closeCarousel = closeCarousel;
window.carouselNav = carouselNav;

document.addEventListener("keydown", event => {
  if ($("#evidence-carousel")?.classList.contains("hidden")) return;
  if (event.key === "Escape") closeCarousel();
  if (event.key === "ArrowLeft") carouselNav(-1);
  if (event.key === "ArrowRight") carouselNav(1);
});
