// ─── WordComet Background Service Worker ────────────────

importScripts("config.js");

// ─── Register with FCM via chrome.gcm ───────────────────

async function registerFCM() {
  return new Promise((resolve, reject) => {
    chrome.gcm.register([CONFIG.GCM_SENDER_ID], async (token) => {
      if (chrome.runtime.lastError) {
        console.error("WordComet: GCM register failed", chrome.runtime.lastError);
        return reject(chrome.runtime.lastError);
      }
      console.log("WordComet: GCM token obtained");
      await chrome.storage.local.set({ gcmToken: token });
      await registerToken(token);
      resolve(token);
    });
  });
}

// ─── Register device token with backend ─────────────────

async function registerToken(token) {
  try {
    await fetch(`${CONFIG.API_BASE}/register-device`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    });
    console.log("WordComet: token registered with backend");
  } catch (e) {
    console.error("WordComet: register failed", e);
  }
}

// ─── First-launch seed fetch (one-time only) ─────────────
// Only called on install so the popup isn't empty before the first push

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
  await chrome.notifications.create("wordcomet-daily", {
    type: "basic",
    iconUrl: "icons/icon128.png",
    title: `☄️ Word of the Day: ${data.word}`,
    message: `(${data.part_of_speech}) ${data.meaning}${data.example ? `\n"${data.example}"` : ""}`,
    priority: 2,
  });
}

// ─── On install ──────────────────────────────────────────

chrome.runtime.onInstalled.addListener(async () => {
  await registerFCM();
  await seedWordOnInstall(); // one-time seed so popup isn't blank
});

// ─── Listen for FCM push (this is the only update path) ──

chrome.gcm.onMessage.addListener(async (message) => {
  const d = message.data;
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

  // Store for popup to read
  await chrome.storage.local.set({ wordData, lastFetch: Date.now() });

  // Show notification
  await showWordNotification(wordData);
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