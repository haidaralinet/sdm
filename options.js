const $ = (id) => document.getElementById(id);
const API = "http://127.0.0.1:16800";

chrome.storage.local.get({ token: "", intercept: true, minMB: 0 }, (c) => {
  $("token").value = c.token;
  $("intercept").checked = c.intercept;
  $("minMB").value = c.minMB;
});

$("save").onclick = async () => {
  const token = $("token").value.trim();
  await chrome.storage.local.set({
    token, intercept: $("intercept").checked, minMB: Number($("minMB").value) || 0,
  });
  const msg = $("msg");
  try {
    const r = await fetch(API + "/add", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Subzeo-Token": token },
      body: "{}",
    });
    msg.textContent = r.ok ? "✔ Connected to Subzeo" : "✖ Wrong token";
    msg.style.color = r.ok ? "#4ade80" : "#f87171";
  } catch (e) {
    msg.textContent = "✖ Subzeo is not running";
    msg.style.color = "#f87171";
  }
};
