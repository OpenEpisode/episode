import { escHtml } from "./dom.js";

let activeDialog = null;

function ensureHost() {
  let host = document.getElementById("dialog-host");
  if (!host) {
    host = document.createElement("div");
    host.id = "dialog-host";
    document.body.appendChild(host);
  }
  return host;
}

export function closeDialog() {
  if (!activeDialog) return;
  activeDialog.remove();
  activeDialog = null;
  document.body.classList.remove("dialog-open");
}

export function openDialog({
  title,
  subtitle = "",
  content,
  submitLabel = "Save",
  danger = false,
  wide = false,
  onSubmit,
}) {
  closeDialog();
  const host = ensureHost();
  const overlay = document.createElement("div");
  overlay.className = "dialog-overlay";
  overlay.innerHTML = `
    <div class="dialog-backdrop" data-dialog-close></div>
    <section class="dialog ${wide ? "dialog-wide" : ""}" role="dialog" aria-modal="true" aria-labelledby="dialog-title">
      <header class="dialog-header">
        <div><h2 id="dialog-title">${escHtml(title)}</h2>${subtitle ? `<p>${escHtml(subtitle)}</p>` : ""}</div>
        <button type="button" class="icon-button" data-dialog-close aria-label="Close">×</button>
      </header>
      <form class="dialog-form">
        <div class="dialog-body">${content}</div>
        <div class="form-error hidden" role="alert"></div>
        <footer class="dialog-footer">
          <button type="button" class="button button-ghost" data-dialog-close>Cancel</button>
          <button type="submit" class="button ${danger ? "button-danger" : "button-primary"}">${escHtml(submitLabel)}</button>
        </footer>
      </form>
    </section>`;
  host.appendChild(overlay);
  activeDialog = overlay;
  document.body.classList.add("dialog-open");
  overlay.querySelectorAll("[data-dialog-close]").forEach(button => {
    button.addEventListener("click", closeDialog);
  });
  const form = overlay.querySelector("form");
  form.addEventListener("submit", async event => {
    event.preventDefault();
    const submit = form.querySelector('[type="submit"]');
    const error = form.querySelector(".form-error");
    submit.disabled = true;
    error.classList.add("hidden");
    try {
      await onSubmit(new FormData(form), form);
    } catch (exception) {
      error.textContent = exception.message;
      error.classList.remove("hidden");
      submit.disabled = false;
    }
  });
  queueMicrotask(() => form.querySelector("input:not([type=hidden]), select")?.focus());
  return overlay;
}

export function confirmDialog({ title, message, confirmLabel = "Delete", onConfirm }) {
  return openDialog({
    title,
    content: `<p class="dialog-message">${escHtml(message)}</p>`,
    submitLabel: confirmLabel,
    danger: true,
    onSubmit: onConfirm,
  });
}

export function notify(message, tone = "success") {
  let host = document.getElementById("toast-host");
  if (!host) {
    host = document.createElement("div");
    host.id = "toast-host";
    host.className = "toast-host";
    document.body.appendChild(host);
  }
  const toast = document.createElement("div");
  toast.className = `toast toast-${tone}`;
  toast.textContent = message;
  host.appendChild(toast);
  setTimeout(() => toast.classList.add("toast-visible"), 10);
  setTimeout(() => {
    toast.classList.remove("toast-visible");
    setTimeout(() => toast.remove(), 200);
  }, 4200);
}
