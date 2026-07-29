/* WildSort — logika rozhraní.
   Celý třídicí cyklus je ovladatelný z klávesnice. Myš je záloha, ne hlavní
   cesta — u 10 000 snímků rozhoduje počet stisků na fotku. Tlačítka dole
   existují pro první seznámení; zkratky jsou v nápovědě (klávesa ?). */

const state = {
  mode: "bursts",      // scenes | bursts | salvage
  scenes: [],
  sceneIndex: -1,
  duel: null,
  bursts: [],
  burstIndex: -1,
  photos: [],
  photoIndex: 0,
  roots: [],
  rootId: null,
  profiles: [],
  hasData: false,      // existuje aspoň jedna fotka v databázi?
  jobWasRunning: false,
};

const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------ pomocné */

async function api(path, options) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail || "Chyba požadavku");
  }
  return res.json();
}

let toastTimer;
function toast(message) {
  const el = $("toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 2800);
}

function formatTime(iso) {
  if (!iso) return "—";
  return iso.slice(11, 19);
}

const STEP_LABELS = {
  import:   ["1/5", "Import souborů"],
  proxy:    ["2/5", "Náhledy"],
  analyze:  ["3/5", "Detekce a metriky"],
  grouping: ["4/5", "Scény a série"],
  scoring:  ["5/5", "Skóre"],
};

/* --------------------------------------------------------- stav úlohy */

async function pollStatus() {
  try {
    const data = await api("/api/status");
    const job = data.job;

    $("detector").textContent = data.detector;
    $("job-msg").textContent = job.message;
    $("job-msg").classList.toggle("error", !!job.error);
    if (job.error) {
      $("job-msg").textContent = "Chyba: " + job.error.split("\n")[0];
      $("job-msg").title = job.error;
    }

    // Ukazatel kroku pipeline: u 6 hodin detekce chce člověk vědět,
    // KTERÝ krok běží a kolik jich ještě zbývá, ne jen že se něco děje.
    const stepInfo = STEP_LABELS[job.step];
    $("job-step").hidden = !(job.running && stepInfo);
    if (job.running && stepInfo) $("job-step").textContent = "krok " + stepInfo[0];

    $("job-track").hidden = !job.running;
    const pct = job.total ? Math.round((job.done / job.total) * 100) : 0;
    $("job-fill").style.width = pct + "%";
    if (job.running && job.total) {
      $("job-msg").textContent = `${job.message} — ${job.done} / ${job.total}`;
    }

    $("btn-import").disabled = job.running;

    state.hasData = (data.stats?.total || 0) > 0;
    updateRoots(data.roots);
    updateWelcome();
    refreshTrust();
    refreshFoot(data.stats);

    // Přechod „běží → doběhlo“: obnov seznamy, ať jsou vidět nové série
    if (state.jobWasRunning && !job.running) {
      toast(job.error ? "Zpracování selhalo" : job.message);
      reloadCurrentMode();
    }
    state.jobWasRunning = job.running;
  } catch (e) {
    $("job-msg").textContent = "Server neodpovídá";
  }
}

function updateRoots(roots) {
  state.roots = roots || [];
  const wrap = $("root-wrap");
  const sel = $("root-select");

  if (state.rootId === null && state.roots.length) {
    state.rootId = state.roots[0].id;
  }

  wrap.hidden = state.roots.length < 2;
  const html = state.roots
    .map((r) => `<option value="${r.id}">${escapeHtml(r.label || r.path)} (${r.photo_count})</option>`)
    .join("");
  if (sel.innerHTML !== html) sel.innerHTML = html;
  if (state.rootId !== null) sel.value = String(state.rootId);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

$("root-select").onchange = (e) => {
  state.rootId = Number(e.target.value);
  reloadCurrentMode();
};

function reloadCurrentMode() {
  state.burstIndex = -1;
  state.sceneIndex = -1;
  if (state.mode === "salvage") loadSalvage();
  else if (state.mode === "scenes") loadScenes();
  else loadBursts();
}

function updateWelcome() {
  // Uvítání jen dokud v databázi nic není; jinak překáží.
  $("welcome").style.display = state.hasData ? "none" : "block";
}

/* ------------------------------------------- souhrn v patičce seznamu */

function refreshFoot(stats) {
  const foot = $("series-foot");
  if (!stats || !stats.total) { foot.hidden = true; return; }
  foot.hidden = false;
  const done = stats.reviewed || 0;
  $("foot-label").textContent = "vyřízeno snímků";
  $("foot-nums").textContent = `${done} / ${stats.total}`;
  $("foot-fill").style.width = (stats.total ? (done / stats.total) * 100 : 0) + "%";
}

/* --------------------------------------------- důvěryhodnost systému */
/* Důvěra v automatiku má být číslo, ne dojem. Měří se průběžně z toho,
   jak často se tvůj výběr shoduje s návrhem — nic navíc dělat nemusíš. */

let trustTimer = 0;

async function refreshTrust() {
  if (Date.now() - trustTimer < 15000) return;   // stačí občas
  trustTimer = Date.now();

  try {
    const cal = await api("/api/calibration" + (state.rootId ? `?root_id=${state.rootId}` : ""));
    const el = $("trust");
    el.className = "trust";

    if (!cal.enough_data) {
      el.textContent = `shoda ${cal.sample}/${cal.min_sample}`;
      el.title = cal.verdict;
      return;
    }
    el.textContent = `shoda ${cal.rate} %`;
    el.title = cal.verdict;
    el.classList.add(cal.rate >= 80 ? "high" : cal.rate >= 60 ? "mid" : "low");
  } catch (e) { /* kalibrace není kritická */ }
}

/* ----------------------------------------------------------- profily */

async function loadProfiles() {
  state.profiles = await api("/api/profiles");
  const sel = $("profile-select");
  sel.innerHTML = state.profiles
    .map((p) => `<option value="${p.name}" title="${escapeHtml(p.note)}">${escapeHtml(p.label)}</option>`)
    .join("");
}

function syncProfileSelect() {
  const burst = state.bursts[state.burstIndex];
  const wrap = $("profile-wrap");
  wrap.hidden = state.mode !== "bursts" || !burst;
  if (burst) $("profile-select").value = burst.profile || "standard";
}

$("profile-select").onchange = async (e) => {
  const burst = state.bursts[state.burstIndex];
  if (!burst) return;

  const result = await api(`/api/burst/${burst.id}/profile`, {
    method: "POST",
    body: JSON.stringify({ profile: e.target.value }),
  });
  burst.profile = result.profile;

  const info = state.profiles.find((p) => p.name === result.profile);
  toast(info ? info.note : "Série přepočítána");

  await openBurst(state.burstIndex);   // ať se projeví nové pořadí
};

/* --------------------------------------------------- přepínání režimů */

function setMode(mode) {
  state.mode = mode;
  for (const m of ["scenes", "bursts", "salvage"]) {
    $(`mode-${m}`).classList.toggle("active", mode === m);
    $(`actions-${m}`).hidden = mode !== m;
  }
  $("filter-unreviewed").parentElement.hidden = mode !== "bursts";
  $("list-caption").textContent =
    mode === "salvage" ? "Od nejhoršího" : mode === "scenes" ? "Situace" : "Podle času";

  hideDuel();
  setZoom(false);
  state.burstIndex = -1;
  state.sceneIndex = -1;
  state.photos = [];
  state.photoIndex = 0;

  if (mode === "salvage") loadSalvage();
  else if (mode === "scenes") loadScenes();
  else loadBursts();
}

$("mode-scenes").onclick = () => setMode("scenes");
$("mode-bursts").onclick = () => setMode("bursts");
$("mode-salvage").onclick = () => setMode("salvage");

function setCount(which, n) {
  $(`count-${which}`).textContent = n == null ? "" : n;
}

/* ------------------------------------------------------------- scény */
/* Scéna je celá situace, ne jedna dávka. Lev u napajedla focený dvacet
   minut je jedna scéna a padesát sérií. Tady je rovnou nejlepší záběr
   z každé situace, místo padesáti téměř stejných vítězů. */

async function loadScenes() {
  const params = new URLSearchParams();
  if (state.rootId) params.set("root_id", state.rootId);

  state.scenes = await api("/api/scenes?" + params);
  setCount("scenes", state.scenes.length);
  renderScenesList();

  if (state.scenes.length) openScene(0);
  else showEmpty("Žádné scény. Nejdřív načti složku a nech proběhnout zpracování.");
}

function renderScenesList() {
  const list = $("series-list");
  list.innerHTML = "";

  state.scenes.forEach((s, i) => {
    const li = document.createElement("li");
    li.className = "series-item scene-item" + (i === state.sceneIndex ? " active" : "") +
                   (s.reviewed ? " done" : "");
    li.innerHTML = `
      <img class="series-thumb" src="/image/${s.best_photo_id}?size=thumb" alt="" loading="lazy">
      <span class="series-time">${formatTime(s.start_time)}</span>
      <span class="scene-bursts" title="${s.burst_count} sérií, ${s.photo_count} snímků">${s.burst_count}× ${s.photo_count}</span>`;
    li.onclick = () => openScene(i);
    list.appendChild(li);
  });
}

async function openScene(index) {
  if (index < 0 || index >= state.scenes.length) return;
  state.sceneIndex = index;

  const detail = await api(`/api/scene/${state.scenes[index].id}`);
  state.photos = detail.winners;
  state.photoIndex = 0;

  renderScenesList();
  renderRidge();
  renderStrip();
  syncProfileSelect();
  showPhoto();
  scrollActiveIntoView();
}

/* ------------------------------------------------------------ souboj */
/* Když jsou první dva snímky série do 5 % skóre, algoritmus mezi nimi
   rozhodnout neumí. Ukázat je vedle sebe je poctivější než vybrat jeden
   a tvářit se, že to bylo jasné. */

async function checkDuel(burstId) {
  const result = await api(`/api/duel/${burstId}`);
  if (!result.duel) { hideDuel(); return false; }

  state.duel = { burstId, ...result.duel };
  const fill = (side, photo) => {
    const el = $(`duel-${side}`);
    el.querySelector("img").src = `/image/${photo.id}`;
    el.querySelector(".duel-name").textContent = photo.filename;
    el.querySelector(".duel-sh").textContent = `ostrost ${(photo.sharpness || 0).toFixed(0)}`;
    el.onclick = () => resolveDuel(photo.id);
  };
  fill("a", result.duel.a);
  fill("b", result.duel.b);
  $("duel").hidden = false;
  return true;
}

function hideDuel() {
  state.duel = null;
  const el = $("duel");
  if (el) el.hidden = true;
}

async function resolveDuel(photoId) {
  if (!state.duel) return;
  const burstId = state.duel.burstId;
  await api(`/api/duel/${burstId}/resolve/${photoId}`, { method: "POST" });
  hideDuel();
  toast("Vítěz souboje vybrán");
  const idx = state.bursts.findIndex((b) => b.id === burstId);
  if (idx >= 0) await openBurst(idx, true);
}

$("duel-skip").onclick = () => hideDuel();

function showEmpty(text) {
  $("main-img").classList.remove("loaded");
  // Dokud v databázi nic není, mluví uvítací panel; text až potom.
  const empty = $("frame-empty");
  empty.hidden = !state.hasData;
  empty.textContent = text;
  $("frame-tags").innerHTML = "";
  $("metrics").innerHTML = "";
  $("ridge").innerHTML = "";
  $("strip").innerHTML = "";
  $("subject-box").hidden = true;
  $("zoom-hint").hidden = true;
  updateWelcome();
}

/* --------------------------------------------------- záchranný režim */

async function loadSalvage() {
  const params = new URLSearchParams();
  if (state.rootId) params.set("root_id", state.rootId);

  state.photos = await api("/api/rejected?" + params);
  state.photoIndex = 0;
  setCount("salvage", state.photos.length);

  renderSalvageList();
  $("ridge").innerHTML = "";
  $("strip").innerHTML = "";
  syncProfileSelect();

  if (state.photos.length) {
    showPhoto();
  } else {
    showEmpty("Žádné zavržené snímky k prohlídce. Buď jsi všechny prošel, nebo ještě nic neproběhlo.");
  }
}

function salvageReason(p) {
  return p.is_empty ? "bez zvířete"
       : (p.sharpness || 0) < 20 ? "rozmaz"
       : "nízké skóre";
}

function renderSalvageList() {
  const list = $("series-list");
  list.innerHTML = "";

  state.photos.forEach((p, i) => {
    const li = document.createElement("li");
    li.className = "series-item salvage-item" + (i === state.photoIndex ? " active" : "");
    li.innerHTML = `
      <img class="series-thumb" src="/image/${p.id}?size=thumb" alt="" loading="lazy">
      <span class="series-time">${escapeHtml(p.filename)}</span>
      <span class="series-count">${salvageReason(p)}</span>`;
    li.onclick = () => { state.photoIndex = i; showPhoto(); renderSalvageList(); };
    list.appendChild(li);
  });
}

async function rescuePhoto(rating) {
  const p = state.photos[state.photoIndex];
  if (!p) return;
  await api("/api/rescue", {
    method: "POST",
    body: JSON.stringify({ photo_id: p.id, rating: rating || 3 }),
  });
  toast(`${p.filename} zachráněn (${"★".repeat(rating || 3)})`);
  removeFromSalvage();
}

async function dismissPhoto() {
  const p = state.photos[state.photoIndex];
  if (!p) return;
  await api(`/api/dismiss/${p.id}`, { method: "POST" });
  removeFromSalvage();
}

function removeFromSalvage() {
  state.photos.splice(state.photoIndex, 1);
  setCount("salvage", state.photos.length);
  if (state.photoIndex >= state.photos.length) {
    state.photoIndex = Math.max(0, state.photos.length - 1);
  }
  renderSalvageList();
  if (state.photos.length) showPhoto();
  else loadSalvage();
}

/* ------------------------------------------------------------- série */

async function loadBursts() {
  const unreviewed = $("filter-unreviewed").checked;
  const params = new URLSearchParams();
  if (state.rootId) params.set("root_id", state.rootId);
  if (unreviewed) params.set("unreviewed_only", "true");

  state.bursts = await api("/api/bursts?" + params);
  setCount("bursts", state.bursts.length);
  renderSeriesList();

  if (state.bursts.length && state.burstIndex < 0) {
    openBurst(0);
  } else if (!state.bursts.length && state.hasData) {
    showEmpty(unreviewed
      ? "Všechny série jsou vyřízené. 🎉 Zkontroluj ještě záložku Zavržené a pak zapiš do XMP."
      : "Žádné série. Počkej, až doběhne zpracování.");
  }
}

function renderSeriesList() {
  const list = $("series-list");
  list.innerHTML = "";

  state.bursts.forEach((b, i) => {
    const li = document.createElement("li");
    li.className = "series-item" + (i === state.burstIndex ? " active" : "") +
                   (b.reviewed ? " done" : "");
    li.innerHTML = `
      <img class="series-thumb" src="/image/${b.best_photo_id}?size=thumb" alt="" loading="lazy">
      <span class="series-time">${formatTime(b.start_time)}</span>
      <span class="series-count" title="${b.photo_count} snímků">${b.photo_count}</span>`;
    li.onclick = () => openBurst(i);
    list.appendChild(li);
  });
}

function scrollActiveIntoView() {
  const el = document.querySelector(".series-item.active");
  if (el) el.scrollIntoView({ block: "nearest" });
}

async function openBurst(index, skipDuel) {
  if (index < 0 || index >= state.bursts.length) return;
  state.burstIndex = index;
  setZoom(false);
  const detail = await api(`/api/burst/${state.bursts[index].id}`);
  state.photos = detail.photos;
  state.bursts[index] = detail.burst;

  // Začni na snímku, který systém považuje za nejlepší
  const bestId = detail.burst.best_photo_id;
  const bestIdx = state.photos.findIndex((p) => p.id === bestId);
  state.photoIndex = bestIdx >= 0 ? bestIdx : 0;

  renderSeriesList();
  renderRidge();
  renderStrip();
  syncProfileSelect();
  showPhoto();
  scrollActiveIntoView();

  hideDuel();
  if (!skipDuel) await checkDuel(detail.burst.id);
}

/* --------------------------------------------------- hřbet ostrosti */

function renderRidge() {
  const ridge = $("ridge");
  ridge.innerHTML = "";
  if (state.photos.length < 2) return;
  const values = state.photos.map((p) => p.sharpness || 0);
  const max = Math.max(...values, 1);

  state.photos.forEach((p, i) => {
    const bar = document.createElement("div");
    bar.className = "ridge-bar";
    const h = Math.max(4, ((p.sharpness || 0) / max) * 100);
    bar.style.height = h + "%";
    if (p.is_empty || (p.auto_rating || 0) <= 1) bar.classList.add("dead");
    if (p.id === state.bursts[state.burstIndex]?.best_photo_id) bar.classList.add("best");
    if (i === state.photoIndex) bar.classList.add("current");
    bar.title = `${p.filename} — ostrost ${(p.sharpness || 0).toFixed(0)}`;
    bar.onclick = () => { state.photoIndex = i; showPhoto(); };
    ridge.appendChild(bar);
  });
}

/* --------------------------------------------------------------- pás */

function renderStrip() {
  const strip = $("strip");
  strip.innerHTML = "";
  const bestId = state.mode === "bursts"
    ? state.bursts[state.burstIndex]?.best_photo_id : null;

  state.photos.forEach((p, i) => {
    const cell = document.createElement("div");
    cell.className = "cell" + (i === state.photoIndex ? " current" : "") +
                     (p.flag ? " " + p.flag : "");
    const mark = p.id === bestId ? `<span class="cell-mark" title="Návrh systému">▲</span>` : "";
    cell.innerHTML = `
      <img src="/image/${p.id}?size=thumb" alt="${escapeHtml(p.filename)}" loading="lazy">
      <span class="cell-stars">${"★".repeat(p.rating || 0)}</span>${mark}`;
    cell.onclick = () => { state.photoIndex = i; showPhoto(); };
    strip.appendChild(cell);
  });
}

/* ---------------------------------------------------- hlavní snímek */

function showPhoto() {
  const p = state.photos[state.photoIndex];
  if (!p) return;

  $("welcome").style.display = "none";
  $("frame-empty").hidden = true;
  $("zoom-hint").hidden = false;
  setZoom(false);

  const img = $("main-img");
  img.classList.remove("loaded");
  img.onload = () => { img.classList.add("loaded"); positionSubjectBox(p); };
  img.src = `/image/${p.id}`;

  renderTags(p);
  renderMetrics(p);
  syncStarButtons(p);

  // zvýraznění v pásu i ve hřbetu bez úplného překreslení
  document.querySelectorAll(".cell").forEach((c, i) =>
    c.classList.toggle("current", i === state.photoIndex));
  document.querySelectorAll(".ridge-bar").forEach((b, i) =>
    b.classList.toggle("current", i === state.photoIndex));

  const cell = document.querySelectorAll(".cell")[state.photoIndex];
  if (cell) cell.scrollIntoView({ block: "nearest", inline: "center" });
}

function positionSubjectBox(p) {
  const box = $("subject-box");
  const img = $("main-img");
  if (p.subject_w == null || p.detection_conf == null) { box.hidden = true; return; }

  const rect = img.getBoundingClientRect();
  const frame = $("frame").getBoundingClientRect();
  box.hidden = false;
  box.style.left   = (rect.left - frame.left + p.subject_x * rect.width) + "px";
  box.style.top    = (rect.top - frame.top + p.subject_y * rect.height) + "px";
  box.style.width  = (p.subject_w * rect.width) + "px";
  box.style.height = (p.subject_h * rect.height) + "px";
}

function renderTags(p) {
  const tags = $("frame-tags");
  tags.innerHTML = "";
  const add = (text, cls) => {
    const el = document.createElement("span");
    el.className = "tag " + (cls || "");
    el.textContent = text;
    tags.appendChild(el);
  };

  add(`${state.photoIndex + 1}/${state.photos.length}`);
  add(p.filename);
  if (p.rating) add("★".repeat(p.rating), "pick");
  if (p.flag === "pick") add("VYBRÁNO", "pick");
  if (p.flag === "reject") add("VYŘAZENO", "reject");
  if (p.is_empty) add("BEZ ZVÍŘETE", "empty");
  if (p.species) add(p.species);
}

function renderMetrics(p) {
  const sharpValues = state.photos.map((x) => x.sharpness || 0);
  const maxSharp = Math.max(...sharpValues, 1);
  const relative = ((p.sharpness || 0) / maxSharp) * 100;

  const items = [
    ["ostrost", (p.sharpness || 0).toFixed(0), relative > 90 ? "good" : (relative < 40 ? "warn" : "")],
  ];

  // Podíl „v sérii" má smysl jen při procházení série. V záchranném
  // režimu je seznam poskládaný napříč celou expedicí a porovnání by
  // lhalo, proto se místo něj ukazuje důvod zavržení.
  if (state.mode === "salvage") {
    items.push(["důvod", salvageReason(p), "warn"]);
  } else {
    items.push(["v sérii", relative.toFixed(0) + " %", relative > 90 ? "good" : ""]);
  }

  items.push(
    ["subjekt", ((p.subject_area || 0) * 100).toFixed(1) + " %", ""],
    ["jas", (p.exposure || 0).toFixed(0), ""],
    ["přepal", ((p.clipped_high || 0) * 100).toFixed(1) + " %", (p.clipped_high || 0) > 0.02 ? "warn" : ""],
    ["ISO", p.iso || "—", ""],
    ["návrh", "★".repeat(p.auto_rating || 0) || "—", ""],
  );

  $("metrics").innerHTML = items.map(([label, value, cls]) =>
    `<div><dt>${label}</dt><dd class="${cls}">${value}</dd></div>`).join("");
}

/* ---------------------------------------------------------- lupa 1:1 */
/* Na rozhodnutí „je ostré oko?" někdy nestačí přizpůsobený náhled.
   Klik nebo Z přepne na skutečnou velikost proxy. */

function setZoom(on) {
  const frame = $("frame");
  const img = $("main-img");
  frame.classList.toggle("zoomed", on);
  if (on) {
    // vycentruj zhruba na subjekt, pokud je znám
    const p = state.photos[state.photoIndex];
    requestAnimationFrame(() => {
      const cx = p && p.subject_x != null ? (p.subject_x + (p.subject_w || 0) / 2) : 0.5;
      const cy = p && p.subject_y != null ? (p.subject_y + (p.subject_h || 0) / 2) : 0.5;
      frame.scrollLeft = img.naturalWidth * cx - frame.clientWidth / 2;
      frame.scrollTop = img.naturalHeight * cy - frame.clientHeight / 2;
    });
  } else if (state.photos[state.photoIndex]) {
    requestAnimationFrame(() => positionSubjectBox(state.photos[state.photoIndex]));
  }
}

function toggleZoom() {
  if (!$("main-img").classList.contains("loaded")) return;
  setZoom(!$("frame").classList.contains("zoomed"));
}

$("main-img").onclick = toggleZoom;

/* -------------------------------------------------------- rozhodnutí */

async function decide(changes) {
  const p = state.photos[state.photoIndex];
  if (!p) return;

  await api("/api/decision", {
    method: "POST",
    body: JSON.stringify({ photo_id: p.id, ...changes }),
  });

  Object.assign(p, changes, { reviewed: 1 });
  renderStrip();
  showPhoto();
}

async function acceptBurst() {
  const burst = state.bursts[state.burstIndex];
  if (!burst) return;
  await api(`/api/accept-burst/${burst.id}`, { method: "POST" });
  burst.reviewed = 1;
  toast("Série přijata podle návrhu");
  nextBurst();
}

function nextPhoto(step) {
  const next = state.photoIndex + step;
  if (next >= 0 && next < state.photos.length) {
    state.photoIndex = next;
    showPhoto();
  }
}

function nextBurst() {
  if (state.mode === "scenes") { openScene(state.sceneIndex + 1); return; }
  if (state.burstIndex + 1 < state.bursts.length) {
    openBurst(state.burstIndex + 1);
  } else {
    toast("Poslední série");
    loadBursts();
  }
}

/* -------------------------------------------------- tlačítka (myš) */

document.querySelectorAll(".star-btn").forEach((btn) => {
  const n = Number(btn.dataset.star);
  btn.onclick = () => {
    if (state.mode === "salvage") rescuePhoto(n);
    else decide({ rating: n, flag: "pick" });
  };
  // podsvícení 1..n při najetí
  btn.onmouseenter = () => document.querySelectorAll(".star-btn").forEach((b) =>
    b.classList.toggle("hover-lit", Number(b.dataset.star) <= n));
  btn.onmouseleave = () => document.querySelectorAll(".star-btn").forEach((b) =>
    b.classList.remove("hover-lit"));
});

function syncStarButtons(p) {
  document.querySelectorAll(".star-btn").forEach((b) =>
    b.classList.toggle("lit", Number(b.dataset.star) <= (p.rating || 0)));
}

$("btn-reject").onclick = () => { decide({ rating: 1, flag: "reject" }); nextPhoto(1); };
$("btn-accept").onclick = () => acceptBurst();
$("btn-rescue").onclick = () => rescuePhoto(3);
$("btn-dismiss").onclick = () => dismissPhoto();
$("btn-open-burst").onclick = () => jumpToBurstOfCurrentPhoto();

/* --------------------------------------------------------- klávesnice */

document.addEventListener("keydown", (e) => {
  if (document.querySelector("dialog[open]")) return;
  if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;

  // Krok zpět platí všude. Při rytmu jednoho stisku za vteřinu je omyl
  // otázkou času.
  if ((e.ctrlKey || e.metaKey) && (e.key === "z" || e.key === "Z")) {
    e.preventDefault();
    undoLast();
    return;
  }

  if (e.key === "?") { e.preventDefault(); $("help-sheet").showModal(); return; }
  if (e.key === "z" || e.key === "Z") { e.preventDefault(); toggleZoom(); return; }

  // Souboj přebírá klávesnici, dokud se nerozhodne
  if (state.duel) {
    if (e.key === "a" || e.key === "A" || e.key === "ArrowLeft") {
      e.preventDefault(); resolveDuel(state.duel.a.id); return;
    }
    if (e.key === "b" || e.key === "B" || e.key === "ArrowRight") {
      e.preventDefault(); resolveDuel(state.duel.b.id); return;
    }
    if (e.key === "Escape") { e.preventDefault(); hideDuel(); return; }
  }

  // Režim scén: procházení nejlepších záběrů jednotlivých situací
  if (state.mode === "scenes") {
    switch (e.key) {
      case "ArrowRight": nextPhoto(1); break;
      case "ArrowLeft":  nextPhoto(-1); break;
      case "ArrowDown":  openScene(state.sceneIndex + 1); break;
      case "ArrowUp":    openScene(state.sceneIndex - 1); break;
      case "1": case "2": case "3": case "4": case "5":
        decide({ rating: Number(e.key), flag: "pick" }); break;
      case "Enter":
        jumpToBurstOfCurrentPhoto(); break;
      default: return;
    }
    e.preventDefault();
    return;
  }

  // Záchranný režim má jiné klávesy: nic se nevyřazuje, jen zachraňuje
  // nebo potvrzuje.
  if (state.mode === "salvage") {
    switch (e.key) {
      case "ArrowRight": case "ArrowDown":
        state.photoIndex = Math.min(state.photos.length - 1, state.photoIndex + 1);
        showPhoto(); renderSalvageList(); break;
      case "ArrowLeft": case "ArrowUp":
        state.photoIndex = Math.max(0, state.photoIndex - 1);
        showPhoto(); renderSalvageList(); break;
      case "r": case "R":
        rescuePhoto(3); break;
      case "1": case "2": case "3": case "4": case "5":
        rescuePhoto(Number(e.key)); break;
      case "x": case "X":
        dismissPhoto(); break;
      default: return;
    }
    e.preventDefault();
    return;
  }

  switch (e.key) {
    case "ArrowRight": nextPhoto(1); break;
    case "ArrowLeft":  nextPhoto(-1); break;
    case "ArrowDown":  nextBurst(); break;
    case "ArrowUp":    openBurst(state.burstIndex - 1); break;
    case "1": case "2": case "3": case "4": case "5":
      decide({ rating: Number(e.key), flag: "pick" }); break;
    case "0":
      decide({ rating: 0, flag: "" }); break;
    case "x": case "X":
      decide({ rating: 1, flag: "reject" }); nextPhoto(1); break;
    case "p": case "P":
      decide({ flag: "pick" }); break;
    case "Enter":
      acceptBurst(); break;
    case " ":
      e.preventDefault(); nextBurst(); break;
    default: return;
  }
  e.preventDefault();
});

/* --------------------------------------------------------- krok zpět */

async function undoLast() {
  const result = await api("/api/undo", { method: "POST" });
  if (!result.ok) { toast(result.message || "Není co vracet"); return; }

  const parts = Object.entries(result.restored || {})
    .map(([k, v]) => `${k}: ${v === "" ? "nic" : v}`).join(", ");
  toast(`Zpět: ${result.filename} (${parts || "obnoveno"})`);

  // Obnov pohled tak, aby bylo vidět, co se změnilo
  if (state.mode === "salvage") {
    loadSalvage();
  } else if (state.mode === "scenes") {
    openScene(state.sceneIndex);
  } else if (result.burst_id) {
    const idx = state.bursts.findIndex((b) => b.id === result.burst_id);
    if (idx >= 0) openBurst(idx, true);
    else loadBursts();
  }
}

/* V režimu scén skočí Enter do série, ze které pochází zobrazený snímek. */
async function jumpToBurstOfCurrentPhoto() {
  const p = state.photos[state.photoIndex];
  if (!p || !p.burst_id) return;

  setMode("bursts");
  await loadBursts();
  const idx = state.bursts.findIndex((b) => b.id === p.burst_id);
  if (idx >= 0) openBurst(idx);
}

/* ----------------------------------------------------------- dialogy */

function openImport() {
  // nabídni dříve načtené složky k rychlému zopakování
  const recent = $("recent-roots");
  const list = $("recent-list");
  list.innerHTML = "";
  recent.hidden = !state.roots.length;
  state.roots.slice(0, 5).forEach((r) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "recent-item";
    b.innerHTML = `${escapeHtml(r.path)}<small>${r.photo_count} snímků</small>`;
    b.onclick = () => { $("import-path").value = r.path; $("import-label").value = r.label || ""; };
    list.appendChild(b);
  });
  $("import-sheet").showModal();
  $("import-path").focus();
}

$("btn-import").onclick = openImport;
$("welcome-import").onclick = openImport;
$("import-cancel").onclick = () => $("import-sheet").close();

$("import-go").onclick = async () => {
  const folder = $("import-path").value.trim();
  if (!folder) { toast("Zadej cestu ke složce"); return; }
  try {
    await api("/api/import", {
      method: "POST",
      body: JSON.stringify({ folder, label: $("import-label").value.trim() || null }),
    });
    $("import-sheet").close();
    state.bursts = [];
    state.burstIndex = -1;
    toast("Zpracování spuštěno — průběh nahoře v liště");
  } catch (e) {
    toast(e.message);
  }
};

$("btn-export").onclick = async () => {
  const q = state.rootId ? `?root_id=${state.rootId}` : "";
  const s = await api("/api/summary" + q);
  $("export-summary").innerHTML = `
    <div class="s-pick"><b>${s.picks}</b><span>vybráno</span></div>
    <div class="s-reject"><b>${s.rejects}</b><span>vyřazeno</span></div>
    <div><b>${s.total - s.reviewed}</b><span>nevyřízeno</span></div>`;

  const notes = [];
  try {
    const cal = await api("/api/calibration" + q);
    notes.push(cal.verdict);
  } catch (e) { /* nepodstatné */ }
  $("export-trust").textContent = notes.join(" ");

  // Duplicity se nezahazují tiše — kdo omylem naimportoval zálohu,
  // chce vidět, CO se překrývá.
  const dupes = $("export-dupes");
  dupes.hidden = !s.duplicates;
  if (s.duplicates) {
    $("export-dupes-cap").textContent =
      `Nalezeno ${s.duplicates} duplicit — do XMP se nezapisují (zobrazit)`;
    try {
      const rows = await api("/api/duplicates" + q);
      $("export-dupes-list").innerHTML = rows.slice(0, 100)
        .map((d) => `<li>${escapeHtml(d.copy_path)} = ${escapeHtml(d.original_path || "?")}</li>`)
        .join("");
    } catch (e) { /* seznam je bonus */ }
  }

  $("export-sheet").showModal();
};

$("export-cancel").onclick = () => $("export-sheet").close();

$("export-go").onclick = async () => {
  $("export-go").disabled = true;
  $("export-go").textContent = "Zapisuji…";
  try {
    const result = await api("/api/export", {
      method: "POST",
      body: JSON.stringify({
        root_id: state.rootId,
        only_reviewed: true,
        move_rejected: $("export-move").checked,
      }),
    });
    $("export-sheet").close();
    if (result.message && !result.written) {
      toast(result.message);
    } else {
      toast(`Zapsáno ${result.written} souborů` +
            (result.failed ? `, chyb ${result.failed} (${result.message || ""})` : "") +
            (result.moved != null ? `, přesunuto ${result.moved}` : ""));
    }
  } catch (e) {
    toast(e.message);
  } finally {
    $("export-go").disabled = false;
    $("export-go").textContent = "Zapsat";
  }
};

$("btn-help").onclick = () => $("help-sheet").showModal();
$("help-close").onclick = () => $("help-sheet").close();

$("filter-unreviewed").onchange = () => {
  state.burstIndex = -1;
  loadBursts();
};

/* -------------------------------------------------------------- start */

pollStatus();
setInterval(pollStatus, 1500);
loadProfiles().catch(() => {});
loadBursts().catch(() => {});

window.addEventListener("resize", () => {
  const p = state.photos[state.photoIndex];
  if (p && !$("frame").classList.contains("zoomed")) positionSubjectBox(p);
});
