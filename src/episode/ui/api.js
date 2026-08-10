import { escapeApiData } from "./dom.js";

export const API = "/api/v1";

export async function api(path) {
  const response = await fetch(API + path);
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return escapeApiData(await response.json());
}

export async function apiBlob(path) {
  const response = await fetch(API + path);
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.blob();
}

export async function apiRequest(path, { method = "GET", body } = {}) {
  const response = await fetch(API + path, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const error = await response.json();
      if (typeof error.detail === "string") message = error.detail;
      else if (Array.isArray(error.detail)) {
        message = error.detail.map(item => item.msg).join(" · ");
      }
    } catch {}
    throw new Error(message);
  }
  if (response.status === 204) return null;
  return escapeApiData(await response.json());
}
