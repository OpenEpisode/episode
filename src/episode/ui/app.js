import { $, $$ } from "./dom.js";
import { alerts } from "./alerts.js";
import { closeCarousel } from "./evidence-gallery.js";
import { captureProfiles, refreshCaptureProfileNotice } from "./capture-profiles.js";
import { areas, devices, deviceView, systemStatus } from "./inventory-pages.js";
import { onboardingNeeded, welcome } from "./onboarding.js";
import { notifications } from "./notifications.js";
import {
  activity,
  closeReviewOverlays,
  episode,
  episodes,
  evidence,
  evidenceDetail,
  event,
} from "./review-pages.js";
import { startSidebar } from "./sidebar.js";
import { cleanupTimeline, renderTimeline } from "./timeline-view.js";
import { startRetentionPolicy } from "./retention-policy.js";
import { toggleCollapse } from "./view.js";

const THEME_STORAGE_KEY = "episode-theme";

function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  localStorage.setItem(THEME_STORAGE_KEY, theme);
  const target = theme === "light" ? "dark" : "light";
  const icon = theme === "light" ? "moon" : "sun";
  $("#theme-toggle").innerHTML = `<svg class="button-icon"><use href="icons.svg#${icon}"></use></svg><span>Use ${target} theme</span>`;
  $("#theme-toggle").setAttribute("aria-label", `Use ${target} theme`);
}

function toggleTheme() {
  const current = document.documentElement.getAttribute("data-theme");
  applyTheme(current === "light" ? "dark" : "light");
}

function closeMobileSidebar() {
  const sidebar = document.querySelector("aside");
  if (!sidebar.classList.contains("open")) return;
  sidebar.classList.remove("open");
  document.getElementById("sidebar-overlay").classList.add("hidden");
  document.body.style.overflow = "";
}

function pageNumber(parameters) {
  const requested = Number.parseInt(parameters.get("page") || "1", 10);
  return Number.isFinite(requested) && requested > 0 ? requested : 1;
}

function navigate() {
  closeReviewOverlays();
  cleanupTimeline();
  if (!$("#evidence-carousel")?.classList.contains("hidden")) closeCarousel();

  const rawHash = location.hash.slice(1) || "timeline";
  const [hash, query = ""] = rawHash.split("?", 2);
  const parameters = new URLSearchParams(query);
  const segments = hash.split("/");
  const view = segments[0];
  const args = segments.slice(1);
  const page = pageNumber(parameters);

  const navigationView = view === "device" ? "devices" : view;
  $$("nav a").forEach(link => {
    link.classList.toggle("active", link.getAttribute("href") === "#" + navigationView);
  });
  closeMobileSidebar();

  if (
    view === "evidence"
    && args.length
    && /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(args[0])
  ) {
    evidenceDetail(args[0]);
    return;
  }

  const routes = {
    episodes: () => episodes(page),
    episode: () => episode(args[0]),
    activity: () => activity(args[0], page, parameters),
    event: () => event(args[0]),
    evidence: () => evidence(args[0], page, parameters),
    devices,
    device: () => deviceView(args[0]),
    areas,
    timeline: () => renderTimeline(),
    system: () => args[0] === "capture-profiles"
      ? captureProfiles()
      : args[0] === "notifications"
      ? notifications()
      : args[0] === "alerts"
      ? alerts(page)
      : systemStatus(args[0]),
    welcome,
  };
  (routes[view] || routes.timeline)();
}

window.toggleTheme = toggleTheme;
window.toggleCollapse = toggleCollapse;
window.toggleSidebar = () => {
  const sidebar = document.querySelector("aside");
  const overlay = document.getElementById("sidebar-overlay");
  const isOpen = sidebar.classList.toggle("open");
  overlay.classList.toggle("hidden", !isOpen);
  document.body.style.overflow = isOpen ? "hidden" : "";
};

applyTheme(localStorage.getItem(THEME_STORAGE_KEY) || "dark");
window.addEventListener("hashchange", navigate);
startSidebar();
window.setInterval(refreshCaptureProfileNotice, 10000);

async function startApplication() {
  try {
    const [, needsOnboarding] = await Promise.all([
      startRetentionPolicy(),
      onboardingNeeded(),
      refreshCaptureProfileNotice(),
    ]);
    const initialView = location.hash.slice(1).split(/[/?]/, 1)[0];
    if (needsOnboarding && (!initialView || initialView === "timeline")) {
      location.hash = "welcome";
      return;
    }
  } catch {
    // Normal page loading will expose any API availability error.
  }
  navigate();
}

startApplication();
