import { escapeApiData } from "./dom.js";

export const API = "/api/v1";

async function errorMessage(response) {
  let message = `${response.status} ${response.statusText}`;
  try {
    const body = await response.json();
    if (typeof body?.error?.message === "string") {
      message = body.error.message;
      const details = Array.isArray(body.error.details)
        ? body.error.details.map(item => item.message).filter(Boolean)
        : [];
      if (details.length) message += ` · ${details.join(" · ")}`;
    } else if (typeof body.detail === "string") {
      message = body.detail;
    } else if (Array.isArray(body.detail)) {
      message = body.detail.map(item => item.msg).join(" · ");
    }
  } catch {}
  return message;
}

export async function api(path) {
  const response = await fetch(API + path);
  if (!response.ok) throw new Error(await errorMessage(response));
  return escapeApiData(await response.json());
}

export async function apiBlob(path) {
  const response = await fetch(API + path);
  if (!response.ok) throw new Error(await errorMessage(response));
  return response.blob();
}

export async function apiAll(path, pageSize = 500) {
  const items = [];
  let offset = 0;
  while (true) {
    const separator = path.includes("?") ? "&" : "?";
    const page = await api(`${path}${separator}limit=${pageSize}&offset=${offset}`);
    items.push(...page);
    if (page.length < pageSize) return items;
    offset += page.length;
  }
}

export async function apiRequest(path, { method = "GET", body } = {}) {
  const response = await fetch(API + path, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(await errorMessage(response));
  }
  if (response.status === 204) return null;
  return escapeApiData(await response.json());
}
