import { $, $$ } from "./dom.js";
import { closeCarousel } from "./evidence-gallery.js?v=6";
import { areas, devices, deviceView, systemStatus } from "./inventory-pages.js?v=6";
import { onboardingNeeded, welcome } from "./onboarding.js?v=5";
import {
  activity,
  closeReviewOverlays,
  episode,
  episodes,
  evidence,
  evidenceDetail,
  event,
} from "./review-pages.js?v=13";
import { startSidebar } from "./sidebar.js?v=3";
import { toggleCollapse } from "./view.js?v=1";

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
  if (!$("#evidence-carousel")?.classList.contains("hidden")) closeCarousel();

  const rawHash = location.hash.slice(1) || "episodes";
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
    system: systemStatus,
    welcome,
  };
  (routes[view] || routes.episodes)();
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

async function startApplication() {
  try {
    const needsOnboarding = await onboardingNeeded();
    const initialView = location.hash.slice(1).split(/[/?]/, 1)[0];
    if (needsOnboarding && (!initialView || initialView === "episodes")) {
      location.hash = "welcome";
      return;
    }
  } catch {
    // Normal page loading will expose any API availability error.
  }
  navigate();
}

startApplication();
