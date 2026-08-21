import { $ } from "./dom.js";

export function showLoading() {
  $("#view-loading").classList.remove("hidden");
  $("#view-error").classList.add("hidden");
  $("#view-content").innerHTML = "";
}

export function showError(message) {
  $("#view-loading").classList.add("hidden");
  const element = $("#view-error");
  element.classList.remove("hidden");
  element.textContent = message;
}

export function showContent(html) {
  $("#view-loading").classList.add("hidden");
  $("#view-error").classList.add("hidden");
  $("#view-content").innerHTML = html;
}

export function toggleCollapse(element) {
  const body = element.nextElementSibling;
  element.classList.toggle("collapsed");
  body.classList.toggle("collapsed");
}
