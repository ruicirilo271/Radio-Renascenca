"use strict";

const CONFIG = window.RADIO_CONFIG;
const HISTORY_KEY = "rr_super_deus_history_v1";
const VOLUME_KEY = "rr_super_deus_volume_v1";
const MAX_HISTORY = 10;
const AUTO_IDENTIFY_MS = 150000;

const audio = document.getElementById("radioAudio");
const mainPlayButton = document.getElementById("mainPlayButton");
const dockPlayButton = document.getElementById("dockPlayButton");
const mainPlayIcon = document.getElementById("mainPlayIcon");
const dockPlayIcon = document.getElementById("dockPlayIcon");
const mainPlayText = document.getElementById("mainPlayText");
const identifyButton = document.getElementById("identifyButton");
const muteButton = document.getElementById("muteButton");
const volumeSlider = document.getElementById("volumeSlider");
const volumeValue = document.getElementById("volumeValue");
const statusLine = document.getElementById("statusLine");
const livePill = document.getElementById("livePill");
const liveLabel = document.getElementById("liveLabel");
const albumFrame = document.getElementById("albumFrame");
const playerDock = document.getElementById("playerDock");
const cover = document.getElementById("cover");
const dockCover = document.getElementById("dockCover");
const trackTitle = document.getElementById("trackTitle");
const trackArtist = document.getElementById("trackArtist");
const trackKicker = document.getElementById("trackKicker");
const dockTitle = document.getElementById("dockTitle");
const dockArtist = document.getElementById("dockArtist");
const streamState = document.getElementById("streamState");
const shazamState = document.getElementById("shazamState");
const shazamDetail = document.getElementById("shazamDetail");
const historyCount = document.getElementById("historyCount");
const historyList = document.getElementById("historyList");
const sessionTime = document.getElementById("sessionTime");
const toast = document.getElementById("toast");
const visualizer = document.getElementById("visualizer");
const visualizerContext = visualizer.getContext("2d");

let isPlaying = false;
let isIdentifying = false;
let autoIdentifyTimer = null;
let firstIdentifyTimer = null;
let sessionTimer = null;
let sessionStartedAt = null;
let toastTimer = null;
let audioContext = null;
let analyser = null;
let sourceNode = null;
let analyserReady = false;
let visualizerAnimation = null;
let lastTrack = null;

const playPath = '<path d="M8 5v14l11-7z"></path>';
const pausePath = '<path d="M7 5h4v14H7zM13 5h4v14h-4z"></path>';

function setStatus(message, type = "neutral") {
  statusLine.classList.remove("success", "error");
  if (type === "success") statusLine.classList.add("success");
  if (type === "error") statusLine.classList.add("error");
  statusLine.querySelector("span:last-child").textContent = message;
}

function showToast(message) {
  window.clearTimeout(toastTimer);
  toast.textContent = message;
  toast.classList.add("visible");
  toastTimer = window.setTimeout(() => toast.classList.remove("visible"), 3200);
}

function updateClock() {
  const now = new Date();
  document.getElementById("clock").textContent = now.toLocaleTimeString("pt-PT", {
    timeZone: "Europe/Lisbon",
    hour: "2-digit",
    minute: "2-digit"
  });
  document.getElementById("dateLabel").textContent = now.toLocaleDateString("pt-PT", {
    timeZone: "Europe/Lisbon",
    day: "2-digit",
    month: "short"
  }).replace(".", "");
}

function updatePlayUI(playing) {
  isPlaying = playing;
  mainPlayIcon.innerHTML = playing ? pausePath : playPath;
  dockPlayIcon.innerHTML = playing ? pausePath : playPath;
  mainPlayText.textContent = playing ? "DESLIGAR RÁDIO" : "LIGAR RÁDIO";
  mainPlayButton.setAttribute("aria-label", playing ? "Desligar rádio" : "Ligar rádio");
  dockPlayButton.setAttribute("aria-label", playing ? "Desligar rádio" : "Ligar rádio");
  livePill.classList.toggle("active", playing);
  albumFrame.classList.toggle("playing", playing);
  playerDock.classList.toggle("playing", playing);
  liveLabel.textContent = playing ? "EM DIRETO" : "PRONTA";
  streamState.textContent = playing ? "ON" : "OFF";

  if (playing) {
    setStatus("Emissão ligada — áudio em direto", "success");
    startSessionTimer();
    scheduleAutomaticIdentification();
  } else {
    setStatus("Emissão em pausa");
    stopSessionTimer();
    clearIdentificationTimers();
  }
}

async function ensureAudioGraph() {
  if (analyserReady) {
    if (audioContext?.state === "suspended") await audioContext.resume();
    return;
  }

  try {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) return;
    audioContext = new AudioContextClass();
    analyser = audioContext.createAnalyser();
    analyser.fftSize = 256;
    analyser.smoothingTimeConstant = 0.83;
    sourceNode = audioContext.createMediaElementSource(audio);
    sourceNode.connect(analyser);
    analyser.connect(audioContext.destination);
    analyserReady = true;
  } catch (error) {
    console.warn("O analisador real não ficou disponível; será usado o modo visual.", error);
    analyserReady = false;
  }
}

async function startRadio() {
  if (!audio.src) audio.src = CONFIG.streamUrl;
  setStatus("A ligar ao stream da Renascença…");

  try {
    await ensureAudioGraph();
    await audio.play();
    updatePlayUI(true);
  } catch (firstError) {
    console.warn("Primeira ligação ao stream falhou; a tentar sem CORS para garantir o áudio.", firstError);
    try {
      audio.pause();
      audio.removeAttribute("crossorigin");
      audio.crossOrigin = null;
      audio.src = CONFIG.streamUrl;
      await audio.play();
      analyserReady = false;
      updatePlayUI(true);
      setStatus("Emissão ligada — equalizador em modo visual", "success");
    } catch (error) {
      console.error(error);
      updatePlayUI(false);
      setStatus("Não foi possível iniciar o stream. Clica novamente.", "error");
      showToast("O navegador bloqueou ou não conseguiu abrir o áudio. Tenta novamente.");
    }
  }
}

function stopRadio() {
  audio.pause();
  // Recarrega o stream ao voltar a ligar, evitando continuar num buffer antigo.
  audio.removeAttribute("src");
  audio.load();
  updatePlayUI(false);
}

function toggleRadio() {
  if (isPlaying) stopRadio();
  else startRadio();
}

function loadSavedVolume() {
  const saved = Number(localStorage.getItem(VOLUME_KEY));
  const volume = Number.isFinite(saved) && saved >= 0 && saved <= 1 ? saved : 0.82;
  audio.volume = volume;
  volumeSlider.value = String(volume);
  volumeValue.textContent = `${Math.round(volume * 100)}%`;
}

function updateVolume(value) {
  const volume = Math.max(0, Math.min(1, Number(value)));
  audio.volume = volume;
  audio.muted = false;
  volumeSlider.value = String(volume);
  volumeValue.textContent = `${Math.round(volume * 100)}%`;
  localStorage.setItem(VOLUME_KEY, String(volume));
}

function toggleMute() {
  audio.muted = !audio.muted;
  muteButton.style.opacity = audio.muted ? ".45" : "1";
  volumeValue.textContent = audio.muted ? "MUDO" : `${Math.round(audio.volume * 100)}%`;
}

function startSessionTimer() {
  if (!sessionStartedAt) sessionStartedAt = Date.now();
  window.clearInterval(sessionTimer);
  sessionTimer = window.setInterval(() => {
    const elapsed = Math.floor((Date.now() - sessionStartedAt) / 1000);
    const minutes = String(Math.floor(elapsed / 60)).padStart(2, "0");
    const seconds = String(elapsed % 60).padStart(2, "0");
    sessionTime.textContent = `${minutes}:${seconds}`;
  }, 1000);
}

function stopSessionTimer() {
  window.clearInterval(sessionTimer);
  sessionTimer = null;
}

function clearIdentificationTimers() {
  window.clearInterval(autoIdentifyTimer);
  window.clearTimeout(firstIdentifyTimer);
  autoIdentifyTimer = null;
  firstIdentifyTimer = null;
}

function scheduleAutomaticIdentification() {
  clearIdentificationTimers();
  firstIdentifyTimer = window.setTimeout(() => identifyTrack(false), 14000);
  autoIdentifyTimer = window.setInterval(() => identifyTrack(false), AUTO_IDENTIFY_MS);
}

async function identifyTrack(force = true) {
  if (isIdentifying) return;
  isIdentifying = true;
  identifyButton.classList.add("loading");
  identifyButton.disabled = true;
  identifyButton.querySelector("span").textContent = "A OUVIR O STREAM…";
  shazamState.textContent = "LISTEN";
  shazamDetail.textContent = "A gravar amostra";
  setStatus("A gravar uma pequena amostra para o Shazam…");

  try {
    const response = await fetch(`/api/identify${force ? "?force=1" : ""}`, {
      method: "POST",
      headers: { "Accept": "application/json" }
    });
    const data = await response.json().catch(() => ({}));

    if (!response.ok || !data.ok) {
      throw new Error(data.error || "Falha ao identificar a música.");
    }

    if (!data.identified) {
      shazamState.textContent = "NO MATCH";
      shazamDetail.textContent = "Tenta mais tarde";
      setStatus(data.message || "Nenhuma música reconhecida nesta amostra.");
      if (force) showToast(data.message || "O Shazam não reconheceu esta amostra.");
      return;
    }

    applyTrack(data);
    addTrackToHistory(data);
    shazamState.textContent = "FOUND";
    shazamDetail.textContent = data.cached ? "Resultado em cache" : `${data.sample_seconds || 8}s analisados`;
    setStatus(`Identificada: ${data.artist} — ${data.title}`, "success");
    showToast(`♪ ${data.artist} — ${data.title}`);
  } catch (error) {
    console.error(error);
    shazamState.textContent = "ERROR";
    shazamDetail.textContent = "Verifica o servidor";
    setStatus(error.message || "Erro durante a identificação.", "error");
    if (force) showToast(error.message || "Não foi possível identificar a música.");
  } finally {
    isIdentifying = false;
    identifyButton.classList.remove("loading");
    identifyButton.disabled = false;
    identifyButton.querySelector("span").textContent = "IDENTIFICAR MÚSICA";
  }
}

function applyTrack(track) {
  lastTrack = track;
  const image = track.cover || track.background || CONFIG.defaultCover;
  trackKicker.textContent = "IDENTIFICADA PELO SHAZAM";
  trackTitle.textContent = track.title || "Música desconhecida";
  trackArtist.textContent = track.album ? `${track.artist} · ${track.album}` : track.artist;
  dockTitle.textContent = track.title || CONFIG.stationName;
  dockArtist.textContent = track.artist || "Emissão em direto";
  setCover(image);
}

function setCover(imageUrl) {
  const safeUrl = imageUrl || CONFIG.defaultCover;
  const preload = new Image();
  preload.onload = () => {
    cover.src = safeUrl;
    dockCover.src = safeUrl;
  };
  preload.onerror = () => {
    cover.src = CONFIG.defaultCover;
    dockCover.src = CONFIG.defaultCover;
  };
  preload.src = safeUrl;
}

function getHistory() {
  try {
    const parsed = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
    return Array.isArray(parsed) ? parsed.slice(0, MAX_HISTORY) : [];
  } catch {
    return [];
  }
}

function addTrackToHistory(track) {
  const history = getHistory();
  const signature = `${track.artist || ""}|${track.title || ""}`.toLowerCase();
  const firstSignature = history[0]
    ? `${history[0].artist || ""}|${history[0].title || ""}`.toLowerCase()
    : "";

  if (signature === firstSignature) {
    history[0] = { ...history[0], ...track, savedAt: new Date().toISOString() };
  } else {
    history.unshift({
      title: track.title,
      artist: track.artist,
      album: track.album || "",
      cover: track.cover || track.background || CONFIG.defaultCover,
      shazam_url: track.shazam_url || "",
      apple_url: track.apple_url || "",
      savedAt: new Date().toISOString()
    });
  }

  localStorage.setItem(HISTORY_KEY, JSON.stringify(history.slice(0, MAX_HISTORY)));
  renderHistory();
}

function renderHistory() {
  const history = getHistory();
  historyCount.textContent = String(history.length);

  if (!history.length) {
    historyList.innerHTML = `
      <div class="empty-state">
        <div>
          <strong>Ainda não existem músicas identificadas</strong>
          <span>Liga a emissão e usa “Identificar música”. As últimas 10 ficam guardadas neste navegador.</span>
        </div>
      </div>`;
    return;
  }

  historyList.innerHTML = history.map((track) => {
    const date = track.savedAt ? new Date(track.savedAt) : new Date();
    const time = date.toLocaleTimeString("pt-PT", { hour: "2-digit", minute: "2-digit" });
    const image = escapeHtml(track.cover || CONFIG.defaultCover);
    return `
      <div class="history-item">
        <img src="${image}" alt="" onerror="this.src='${escapeHtml(CONFIG.defaultCover)}'">
        <div class="history-meta">
          <strong>${escapeHtml(track.title || "Música desconhecida")}</strong>
          <span>${escapeHtml(track.artist || "Artista desconhecido")}${track.album ? ` · ${escapeHtml(track.album)}` : ""}</span>
        </div>
        <span class="history-time">${time}</span>
      </div>`;
  }).join("");
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function clearHistory() {
  localStorage.removeItem(HISTORY_KEY);
  renderHistory();
  showToast("Histórico apagado.");
}

async function copyCurrentTrack() {
  const text = lastTrack
    ? `${lastTrack.artist} — ${lastTrack.title}`
    : `${CONFIG.stationName} — Emissão em direto`;
  try {
    await navigator.clipboard.writeText(text);
    showToast("Nome copiado para a área de transferência.");
  } catch {
    showToast(text);
  }
}

function resizeVisualizer() {
  const rect = visualizer.getBoundingClientRect();
  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  visualizer.width = Math.max(1, Math.round(rect.width * ratio));
  visualizer.height = Math.max(1, Math.round(rect.height * ratio));
  visualizerContext.setTransform(ratio, 0, 0, ratio, 0, 0);
}

function drawVisualizer() {
  const width = visualizer.clientWidth;
  const height = visualizer.clientHeight;
  visualizerContext.clearRect(0, 0, width, height);

  const bars = width < 600 ? 38 : 72;
  const gap = width < 600 ? 3 : 4;
  const barWidth = Math.max(2, (width - gap * (bars - 1)) / bars);
  let values = null;

  if (analyserReady && analyser) {
    const raw = new Uint8Array(analyser.frequencyBinCount);
    analyser.getByteFrequencyData(raw);
    values = Array.from({ length: bars }, (_, index) => {
      const rawIndex = Math.floor(index / bars * raw.length);
      return raw[rawIndex] / 255;
    });
  }

  const time = performance.now() / 620;
  for (let i = 0; i < bars; i += 1) {
    let strength;
    if (isPlaying && values) {
      strength = Math.max(.055, values[i]);
    } else if (isPlaying) {
      strength = .16 + Math.abs(Math.sin(time + i * .31)) * .52 + Math.random() * .12;
    } else {
      strength = .045 + Math.abs(Math.sin(time * .28 + i * .18)) * .055;
    }

    const barHeight = Math.max(5, strength * (height - 15));
    const x = i * (barWidth + gap);
    const y = height - barHeight;
    const gradient = visualizerContext.createLinearGradient(0, y, 0, height);
    gradient.addColorStop(0, "rgba(255, 217, 139, .98)");
    gradient.addColorStop(.48, "rgba(247, 185, 85, .88)");
    gradient.addColorStop(1, "rgba(228, 49, 67, .55)");
    visualizerContext.fillStyle = gradient;
    visualizerContext.shadowColor = "rgba(228, 49, 67, .34)";
    visualizerContext.shadowBlur = isPlaying ? 9 : 2;
    roundRect(visualizerContext, x, y, barWidth, barHeight, Math.min(4, barWidth / 2));
    visualizerContext.fill();
  }

  visualizerContext.shadowBlur = 0;
  visualizerAnimation = requestAnimationFrame(drawVisualizer);
}

function roundRect(ctx, x, y, width, height, radius) {
  const r = Math.min(radius, width / 2, height / 2);
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + width, y, x + width, y + height, r);
  ctx.arcTo(x + width, y + height, x, y + height, r);
  ctx.arcTo(x, y + height, x, y, r);
  ctx.arcTo(x, y, x + width, y, r);
  ctx.closePath();
}

function startStarfield() {
  const canvas = document.getElementById("starfield");
  const ctx = canvas.getContext("2d");
  let particles = [];

  function resize() {
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = window.innerWidth * ratio;
    canvas.height = window.innerHeight * ratio;
    canvas.style.width = `${window.innerWidth}px`;
    canvas.style.height = `${window.innerHeight}px`;
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    particles = Array.from({ length: Math.min(90, Math.floor(window.innerWidth / 14)) }, () => ({
      x: Math.random() * window.innerWidth,
      y: Math.random() * window.innerHeight,
      r: Math.random() * 1.3 + .25,
      speed: Math.random() * .11 + .025,
      alpha: Math.random() * .45 + .08
    }));
  }

  function render() {
    ctx.clearRect(0, 0, window.innerWidth, window.innerHeight);
    for (const p of particles) {
      p.y -= p.speed;
      if (p.y < -3) {
        p.y = window.innerHeight + 3;
        p.x = Math.random() * window.innerWidth;
      }
      ctx.beginPath();
      ctx.fillStyle = `rgba(255, 220, 170, ${p.alpha})`;
      ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
      ctx.fill();
    }
    requestAnimationFrame(render);
  }

  resize();
  window.addEventListener("resize", resize, { passive: true });
  render();
}

async function checkServerStatus() {
  try {
    const response = await fetch("/api/status", { headers: { "Accept": "application/json" } });
    const data = await response.json();
    if (!data.ffmpeg_ready) {
      shazamState.textContent = "SETUP";
      shazamDetail.textContent = "FFmpeg em falta";
    } else {
      shazamDetail.textContent = `${data.identify_seconds}s por amostra`;
    }
  } catch {
    shazamState.textContent = "OFFLINE";
    shazamDetail.textContent = "API indisponível";
  }
}

mainPlayButton.addEventListener("click", toggleRadio);
dockPlayButton.addEventListener("click", toggleRadio);
identifyButton.addEventListener("click", () => identifyTrack(true));
volumeSlider.addEventListener("input", (event) => updateVolume(event.target.value));
muteButton.addEventListener("click", toggleMute);
document.getElementById("clearHistoryButton").addEventListener("click", clearHistory);
document.getElementById("copyTrackButton").addEventListener("click", copyCurrentTrack);

cover.addEventListener("error", () => { cover.src = CONFIG.defaultCover; });
dockCover.addEventListener("error", () => { dockCover.src = CONFIG.defaultCover; });

audio.addEventListener("playing", () => updatePlayUI(true));
audio.addEventListener("pause", () => {
  if (isPlaying) updatePlayUI(false);
});
audio.addEventListener("stalled", () => {
  if (isPlaying) setStatus("O stream está a recuperar a ligação…");
});
audio.addEventListener("error", () => {
  updatePlayUI(false);
  setStatus("O stream não respondeu. Tenta ligar novamente.", "error");
});

window.addEventListener("resize", resizeVisualizer, { passive: true });
window.addEventListener("beforeunload", () => {
  if (visualizerAnimation) cancelAnimationFrame(visualizerAnimation);
});

loadSavedVolume();
renderHistory();
updateClock();
window.setInterval(updateClock, 1000);
resizeVisualizer();
drawVisualizer();
startStarfield();
checkServerStatus();
