/* global chrome, browser */
const isFirefoxPromiseApi = typeof browser !== "undefined" && browser.cookies && browser.cookies.getAll;
const api = isFirefoxPromiseApi ? browser : chrome;
const COOKIE_NAMES = ["cf_clearance", "_danbooru2_session"];
const DONMAI_RE = /(^|\.)donmai\.us$/i;
const RUNTIME_TTL_MS = 30 * 60 * 1000;

const $ = (id) => document.getElementById(id);
let currentCookieHeader = "";
let currentCookies = [];
let capturedAt = 0;
let source = "none";
let currentStatusTimer = null;

function callChrome(fn, arg) {
  return new Promise((resolve, reject) => {
    try {
      fn(arg, (result) => {
        const err = chrome.runtime && chrome.runtime.lastError;
        if (err) reject(new Error(err.message));
        else resolve(result);
      });
    } catch (e) { reject(e); }
  });
}
function sendMessage(msg) {
  return new Promise((resolve, reject) => {
    try {
      chrome.runtime.sendMessage(msg, (result) => {
        const err = chrome.runtime && chrome.runtime.lastError;
        if (err) reject(new Error(err.message));
        else resolve(result || {});
      });
    } catch (e) { reject(e); }
  });
}
function getAll(query) {
  if (isFirefoxPromiseApi) return api.cookies.getAll(query);
  return callChrome(chrome.cookies.getAll, query);
}
function getStores() {
  if (isFirefoxPromiseApi && api.cookies.getAllCookieStores) return api.cookies.getAllCookieStores();
  if (chrome.cookies && chrome.cookies.getAllCookieStores) {
    return new Promise((resolve) => chrome.cookies.getAllCookieStores(resolve));
  }
  return Promise.resolve([{ id: undefined }]);
}
function normalizeDomain(domain) { return String(domain || "").replace(/^\./, "").toLowerCase(); }
function isDanbooruCookie(cookie) {
  const domain = normalizeDomain(cookie.domain);
  return DONMAI_RE.test(domain) && COOKIE_NAMES.includes(cookie.name);
}
function uniqueCookies(cookies) {
  const map = new Map();
  for (const c of cookies) map.set(`${c.name};${c.domain};${c.path};${c.storeId || ""}`, c);
  return [...map.values()];
}
function cookieHeaderHas(name, header = currentCookieHeader) {
  return new RegExp(`(^|;\\s*)${name}=`).test(header || "");
}
function selectFromCookieApi(all) {
  all = uniqueCookies(all).filter(isDanbooruCookie);
  const selected = [];
  for (const name of COOKIE_NAMES) {
    const candidates = all.filter(c => c.name === name);
    candidates.sort((a, b) => {
      const ad = normalizeDomain(a.domain) === "danbooru.donmai.us" ? 1 : 0;
      const bd = normalizeDomain(b.domain) === "danbooru.donmai.us" ? 1 : 0;
      if (ad !== bd) return bd - ad;
      return (b.expirationDate || 0) - (a.expirationDate || 0);
    });
    if (candidates[0]) selected.push(candidates[0]);
  }
  return selected;
}
async function readCookieApiHeader() {
  let all = [];
  const stores = await getStores().catch(() => [{ id: undefined }]);
  const queries = [];
  for (const store of stores) {
    for (const name of COOKIE_NAMES) {
      const q = { name }; if (store.id) q.storeId = store.id; queries.push(q);
    }
  }
  for (const store of stores) {
    for (const url of ["https://danbooru.donmai.us/", "https://danbooru.donmai.us/posts", "https://donmai.us/"]) {
      const q = { url }; if (store.id) q.storeId = store.id; queries.push(q);
    }
  }
  for (const q of queries) { try { all.push(...await getAll(q)); } catch (_) {} }
  currentCookies = selectFromCookieApi(all);
  return currentCookies.map(c => `${c.name}=${c.value}`).join("; ");
}
async function readCookies() {
  setStatus("Читаю latest request cookie…", "");
  const latest = await sendMessage({ type: "getLatestCookieHeader" }).catch(() => ({}));
  const requestHeader = latest.latestDanbooruCookieHeader || "";
  const apiHeader = await readCookieApiHeader().catch(() => "");

  // Prefer actual request Cookie header: it is exactly what DevTools shows and includes HttpOnly cookies.
  if (cookieHeaderHas("cf_clearance", requestHeader) || cookieHeaderHas("_danbooru2_session", requestHeader)) {
    currentCookieHeader = requestHeader;
    capturedAt = latest.latestDanbooruCookieCapturedAt || Date.now();
    source = "request";
  } else {
    currentCookieHeader = apiHeader;
    capturedAt = Date.now();
    source = "cookies";
  }
  render();
}
function runtimeTimerText() {
  if (!capturedAt || !cookieHeaderHas("cf_clearance")) return "missing";
  const delta = capturedAt + RUNTIME_TTL_MS - Date.now();
  if (delta <= 0) return "expired";
  const min = Math.floor(delta / 60000);
  const sec = Math.floor((delta % 60000) / 1000);
  return `${min}m ${sec}s`;
}
function setState(el, ok, text) { el.textContent = text; el.className = ok ? "ok" : "bad"; }
function setStatus(text, cls) {
  const status = $("status"); status.textContent = text; status.className = cls ? `status ${cls}` : "status";
}
function render() {
  const hasCf = cookieHeaderHas("cf_clearance");
  const hasSession = cookieHeaderHas("_danbooru2_session");
  setState($("cfState"), hasCf, hasCf ? "found" : "missing");
  setState($("sessionState"), hasSession, hasSession ? "found" : "missing");
  const timer = runtimeTimerText();
  $("timerState").textContent = timer;
  $("timerState").className = timer === "expired" || timer === "missing" ? "bad" : "ok";
  $("sourceState").textContent = source;
  $("sourceState").className = source === "request" ? "ok" : (source === "cookies" ? "warn" : "bad");
  $("cookieBox").value = currentCookieHeader;
  if (hasCf && hasSession) setStatus("Ready: request cookie готова.", "ok");
  else if (hasCf || hasSession) setStatus("Partial: не хватает одной cookie. Обнови Danbooru и нажми Refresh.", "warn");
  else setStatus("Открой/обнови Danbooru, чтобы расширение поймало request Cookie header.", "bad");
}
async function copyText(text) {
  if (!text) throw new Error("Nothing to copy");
  if (navigator.clipboard && navigator.clipboard.writeText) { await navigator.clipboard.writeText(text); return; }
  const box = $("cookieBox"); box.value = text; box.focus(); box.select(); document.execCommand("copy");
}
function flash(message, cls = "ok") {
  if (currentStatusTimer) clearTimeout(currentStatusTimer);
  setStatus(message, cls);
  currentStatusTimer = setTimeout(render, 1200);
}
$("copyCookie").addEventListener("click", async () => {
  try { await copyText(currentCookieHeader); flash("Cookie copied."); } catch (e) { flash(e.message, "bad"); }
});
$("refresh").addEventListener("click", async () => {
  try { await readCookies(); flash("Refreshed."); } catch (e) { flash(`Refresh failed: ${e.message}`, "bad"); }
});
$("openDanbooru").addEventListener("click", () => { api.tabs.create({ url: "https://danbooru.donmai.us/posts" }); });
readCookies().catch(e => setStatus(`Error: ${e.message}`, "bad"));
setInterval(() => { if (currentCookieHeader) render(); }, 1000);
