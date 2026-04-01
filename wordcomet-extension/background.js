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
      await chrome.storage.local.set({ gcmToken: token });
      await registerToken(token);
      resolve(token);
    });
  });
}

// Fetch word from backend
async function fetchWord() {
  try {
    const res = await fetch(`${CONFIG.API_BASE}/word-of-the-day`);
    if (!res.ok) return null;
    const data = await res.json();
    await chrome.storage.local.set({ wordData: data, lastFetch: Date.now() });
    return data;
  } catch (e) {
    console.error("WordComet: fetch failed", e);
    return null;
  }
}

// Register device token with backend
async function registerToken(token) {
  try {
    await fetch(`${CONFIG.API_BASE}/register-device`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    });
    console.log("WordComet: device registered");
  } catch (e) {
    console.error("WordComet: register failed", e);
  }
}

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

// On install — fetch immediately
chrome.runtime.onInstalled.addListener(async () => {
  await registerFCM();
  const data = await fetchWord();
  if (data) await showWordNotification(data);
  chrome.alarms.create("fetchWord", { periodInMinutes: 60 });
  chrome.alarms.create("dailyNotification", {
    when: getNext8AM(),
    periodInMinutes: 24 * 60,
  });
});

// On alarm — periodic fetch
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "fetchWord") {
    fetchWord();
  }
});

// ─── Listen for FCM push ─────────────────────────────────

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

  await chrome.storage.local.set({ wordData, lastFetch: Date.now() });
  await showWordNotification(wordData);
});

// ─── On notification click → open popup ─────────────────

chrome.notifications.onClicked.addListener(() => {
  chrome.action.openPopup().catch(() => {});
});

// ─── Helper ──────────────────────────────────────────────

function getNext8AM() {
  const now = new Date();
  const next = new Date();
  next.setHours(8, 0, 0, 0);
  if (next <= now) next.setDate(next.getDate() + 1);
  return next.getTime();
}

// Listen for messages from popup
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.action === "fetchWord") {
    fetchWord().then((data) => sendResponse(data));
    return true; // async
  }
});
