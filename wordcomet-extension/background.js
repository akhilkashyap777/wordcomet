// ─── WordComet Background Service Worker ────────────────

importScripts("config.js");

// ─── Helper: Base64 URL to Uint8Array ───────────────────

function urlBase64ToUint8Array(base64String) {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(base64);
  const arr = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);
  return arr;
}

// ─── Subscribe to Web Push ──────────────────────────────

async function subscribePush() {
  try {
    const sub = await self.registration.pushManager.subscribe({
      userVisibleOnly: false,
      applicationServerKey: urlBase64ToUint8Array(CONFIG.VAPID_KEY),
    });
    console.log("WordComet: Web Push subscribed");
    await registerSubscription(sub);
    return sub;
  } catch (e) {
    console.error("WordComet: Push subscribe failed", e);
  }
}

// ─── Register subscription with backend ─────────────────

async function registerSubscription(sub) {
  try {
    await fetch(`${CONFIG.API_BASE}/register-device`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ subscription: sub.toJSON() }),
    });
    console.log("WordComet: subscription registered with backend");
  } catch (e) {
    console.error("WordComet: register failed", e);
  }
}

// ─── First-launch seed fetch ─────────────────────────────

async function seedWordOnInstall() {
  try {
    const res = await fetch(`${CONFIG.API_BASE}/word-of-the-day`);
    if (!res.ok) return;
    const data = await res.json();
    await chrome.storage.local.set({ wordData: data, lastFetch: Date.now() });
  } catch (e) {
    console.error("WordComet: seed fetch failed", e);
  }
}

// ─── Show rich notification ──────────────────────────────

async function showWordNotification(data) {
  if (!data) return;
  await self.registration.showNotification(
    `☄️ Word of the Day: ${data.word}`,
    {
      body: `(${data.part_of_speech}) ${data.meaning}`,
      icon: "icons/icon128.png",
    }
  );
}

// ─── On install ──────────────────────────────────────────

chrome.runtime.onInstalled.addListener(async () => {
  await subscribePush();
  await seedWordOnInstall();
});

// ─── Listen for Web Push ─────────────────────────────────

self.addEventListener("push", (event) => {
  let d;
  try { d = event.data.json(); } catch { return; }
  if (!d || !d.word) return;

  const wordData = {
    word: d.word,
    meaning: d.meaning,
    part_of_speech: d.part_of_speech,
    example: d.example || null,
    pronunciation: d.pronunciation || null,
    phonetics: d.phonetics || null,
    language: d.language || "English",
    date: d.date || new Date().toISOString().split("T")[0],
  };

  event.waitUntil(
    (async () => {
      await chrome.storage.local.set({ wordData, lastFetch: Date.now() });
      await showWordNotification(wordData);
    })()
  );
});

// ─── On notification click → open popup ─────────────────

chrome.notifications.onClicked.addListener(() => {
  chrome.action.openPopup().catch(() => {});
});

// ─── Listen for messages from popup ─────────────────────

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.action === "getWord") {
    chrome.storage.local.get(["wordData"]).then((result) => {
      sendResponse(result.wordData || null);
    });
    return true;
  }
});