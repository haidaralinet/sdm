// Subzeo browser integration (Chrome / Edge / Brave, Manifest V3)
const API = "http://127.0.0.1:16800";

const cfg = () => chrome.storage.local.get({ token: "", intercept: true, minMB: 0 });

async function ping() {
  try {
    const r = await fetch(API + "/", { method: "GET" });
    return (await r.json()).app === "subzeo";
  } catch (e) {
    return false;
  }
}

async function send(payload) {
  const { token } = await cfg();
  if (!token) return false;
  try {
    payload.cookie = payload.cookie || (await cookieFor(payload.url));
    const r = await fetch(API + "/add", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Subzeo-Token": token },
      body: JSON.stringify(payload),
    });
    return r.ok;
  } catch (e) {
    return false;
  }
}

async function cookieFor(url) {
  try {
    const list = await chrome.cookies.getAll({ url });
    return list.map((c) => c.name + "=" + c.value).join("; ");
  } catch (e) {
    return "";
  }
}

// Take over normal browser downloads
chrome.downloads.onCreated.addListener(async (item) => {
  const c = await cfg();
  if (!c.intercept || !c.token || item.byExtensionId) return;
  const url = item.finalUrl || item.url;
  if (!/^https?:/i.test(url)) return;
  if (c.minMB && item.fileSize > 0 && item.fileSize < c.minMB * 1048576) return;
  if (!(await ping())) return; // Subzeo not running: let the browser download it
  const ok = await send({ url, referer: item.referrer || "", filename: "" });
  if (ok) {
    chrome.downloads.cancel(item.id, () => chrome.downloads.erase({ id: item.id }));
  }
});

// Right-click menu
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "subzeo-link", title: "Download with Subzeo",
    contexts: ["link", "video", "audio", "image"],
  });
  chrome.contextMenus.create({
    id: "subzeo-video", title: "Download this video with Subzeo (yt-dlp)",
    contexts: ["page", "video"],
  });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId === "subzeo-link") {
    const url = info.linkUrl || info.srcUrl;
    if (url) await send({ url, referer: info.pageUrl || (tab && tab.url) || "" });
  } else if (info.menuItemId === "subzeo-video") {
    await send({ kind: "video", url: info.pageUrl || (tab && tab.url) });
  }
});
