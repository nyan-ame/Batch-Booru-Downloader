/* global chrome, browser */
const api = typeof browser !== "undefined" ? browser : chrome;
const STORAGE_KEY = "latestDanbooruCookieHeader";
const TIME_KEY = "latestDanbooruCookieCapturedAt";

function chromeStorageSet(obj) {
  return new Promise((resolve) => chrome.storage.local.set(obj, resolve));
}
function chromeStorageGet(keys) {
  return new Promise((resolve) => chrome.storage.local.get(keys, resolve));
}
function storageSet(obj) {
  if (typeof browser !== "undefined" && browser.storage && browser.storage.local) return browser.storage.local.set(obj);
  return chromeStorageSet(obj);
}
function storageGet(keys) {
  if (typeof browser !== "undefined" && browser.storage && browser.storage.local) return browser.storage.local.get(keys);
  return chromeStorageGet(keys);
}

function extractCookieHeader(details) {
  const headers = details.requestHeaders || [];
  const h = headers.find(x => String(x.name || "").toLowerCase() === "cookie");
  return h ? h.value || "" : "";
}

function hasUsefulDanbooruCookie(header) {
  return /(^|;\s*)cf_clearance=/.test(header) || /(^|;\s*)_danbooru2_session=/.test(header);
}

async function rememberCookieHeader(header) {
  if (!header || !hasUsefulDanbooruCookie(header)) return;
  await storageSet({
    [STORAGE_KEY]: header,
    [TIME_KEY]: Date.now()
  });
}

try {
  chrome.webRequest.onBeforeSendHeaders.addListener(
    (details) => {
      const header = extractCookieHeader(details);
      rememberCookieHeader(header);
    },
    { urls: ["https://danbooru.donmai.us/*", "https://*.donmai.us/*"] },
    ["requestHeaders", "extraHeaders"]
  );
} catch (e) {
  // Firefox may not support extraHeaders.
  chrome.webRequest.onBeforeSendHeaders.addListener(
    (details) => {
      const header = extractCookieHeader(details);
      rememberCookieHeader(header);
    },
    { urls: ["https://danbooru.donmai.us/*", "https://*.donmai.us/*"] },
    ["requestHeaders"]
  );
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || msg.type !== "getLatestCookieHeader") return false;
  storageGet([STORAGE_KEY, TIME_KEY]).then(sendResponse);
  return true;
});
