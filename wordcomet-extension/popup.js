// ─── WordComet Popup Logic ───────────────────────────────

const $ = (sel) => document.querySelector(sel);

const elLoading  = $("#loading");
const elNoWord   = $("#no-word");
const elWordCard = $("#word-card");

// ─── Show State ──────────────────────────────────────────

function showState(state) {
  elLoading.classList.add("hidden");
  elNoWord.classList.add("hidden");
  elWordCard.classList.add("hidden");
  state.classList.remove("hidden");
}

// ─── Render Word ─────────────────────────────────────────

function renderWord(data) {
  const d = data.date ? new Date(data.date + "T00:00:00") : new Date();
  $("#word-date").textContent = d.toLocaleDateString("en-US", {
    month: "short", day: "numeric", year: "numeric"
  });

  $("#word-text").textContent = data.word;
  $("#word-pos").textContent = data.part_of_speech;
  $("#word-lang").textContent = data.language || "English";

  const pronSection = $("#pronunciation-section");
  if (data.pronunciation || data.phonetics) {
    pronSection.classList.remove("hidden");
    $("#word-pronunciation").textContent = data.pronunciation || "";
    $("#word-phonetics").textContent = data.phonetics || "";
  } else {
    pronSection.classList.add("hidden");
  }

  $("#word-meaning").textContent = data.meaning;

  const exSection = $("#example-section");
  if (data.example) {
    exSection.classList.remove("hidden");
    $("#word-example").textContent = `"${data.example}"`;
  } else {
    exSection.classList.add("hidden");
  }

  showState(elWordCard);
}

// ─── Load Word ───────────────────────────────────────────
// Primary source: chrome.storage.local (written by FCM push in background.js)
// Fallback: direct fetch — only used when storage is empty (first launch / no push yet)

async function loadWord(forceRefetch = false) {
  showState(elLoading);

  const cached = await chrome.storage.local.get(["wordData"]);

  if (cached.wordData && !forceRefetch) {
    // FCM already pushed a word — render it immediately, no fetch needed
    renderWord(cached.wordData);
    return;
  }

  // First launch or manual retry: fetch once to seed storage
  try {
    const res = await fetch(`${CONFIG.API_BASE}/word-of-the-day`);
    if (res.ok) {
      const data = await res.json();
      await chrome.storage.local.set({ wordData: data, lastFetch: Date.now() });
      renderWord(data);
    } else {
      showState(elNoWord);
    }
  } catch (e) {
    showState(elNoWord);
  }
}

// ─── Copy Button ─────────────────────────────────────────

$("#copy-btn").addEventListener("click", async () => {
  const cached = await chrome.storage.local.get(["wordData"]);
  if (!cached.wordData) return;

  const w = cached.wordData;
  const text = `${w.word} (${w.part_of_speech}) — ${w.meaning}${w.example ? `\nExample: "${w.example}"` : ""}`;

  await navigator.clipboard.writeText(text);

  const btn = $("#copy-btn");
  const label = $("#copy-label");
  btn.classList.add("copied");
  label.textContent = "Copied!";

  setTimeout(() => {
    btn.classList.remove("copied");
    label.textContent = "Copy";
  }, 1500);
});

// ─── Share Button ────────────────────────────────────────

$("#share-btn").addEventListener("click", async () => {
  const cached = await chrome.storage.local.get(["wordData"]);
  if (!cached.wordData) return;

  const w = cached.wordData;
  const text = `☄️ Word of the Day: ${w.word}\n${w.meaning}\n— WordComet`;

  try {
    await navigator.clipboard.writeText(text);
    const btn = $("#share-btn");
    btn.classList.add("copied");
    btn.querySelector("span")?.remove();
    btn.insertAdjacentHTML("beforeend", "<span>Copied to share!</span>");
    setTimeout(() => {
      btn.classList.remove("copied");
      const s = btn.querySelector("span");
      if (s) s.textContent = "Share";
    }, 1500);
  } catch {}
});

// ─── Retry Button ────────────────────────────────────────

$("#retry-btn").addEventListener("click", () => loadWord(true));

// ─── Init ────────────────────────────────────────────────

loadWord();