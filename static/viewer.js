import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';

// ─── Three.js scene ──────────────────────────────────────────────────────────

const canvas   = document.getElementById("c");
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
renderer.setPixelRatio(window.devicePixelRatio);

const scene  = new THREE.Scene();
scene.background = new THREE.Color(0x0b0f1a);

const camera = new THREE.PerspectiveCamera(45, 1, 0.01, 10000);
camera.position.set(5, 5, 10);

const controls = new OrbitControls(camera, canvas);
controls.enableDamping = true;
controls.dampingFactor = 0.08;

scene.add(new THREE.AmbientLight(0xffffff, 0.5));
const dirLight = new THREE.DirectionalLight(0xffffff, 1.0);
dirLight.position.set(10, 20, 10);
scene.add(dirLight);

function resize() {
  const w = canvas.parentElement.clientWidth;
  const h = canvas.parentElement.clientHeight;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
window.addEventListener("resize", resize);
resize();

(function loop() {
  requestAnimationFrame(loop);
  controls.update();
  renderer.render(scene, camera);
})();

// ─── State ───────────────────────────────────────────────────────────────────

// selectedComponents: [{ifcGuid, color}] — fixed once topic is loaded
let selectedComponents = [];
// allVersions: [{id, fileName, exportedAt}] sorted oldest→newest (index 0 = v1)
let allVersions = [];
let currentVersionIdx = -1;
let bcfCamera = null;
let sceneMeshes = [];
let meshByGuid   = {}; // { ifcGuid: THREE.Mesh }
let selectedGuid = null;
let lastModifiedMap  = {}; // { ifcGuid: { fileName, version } }

// ─── Helpers ─────────────────────────────────────────────────────────────────

function setStatus(msg, cls = "") {
  const el = document.getElementById("status");
  el.textContent = msg;
  el.className = cls;
}

function bcfColorToHex(argb) {
  if (!argb) return null;
  const s = argb.replace("#", "");
  const rgb = s.length === 8 ? s.slice(2) : s;
  return parseInt(rgb, 16);
}

const _gltfLoader = new GLTFLoader();

// Decode a base64 GLB from the IfcMesh.glb field and return a THREE.Mesh.
// The coordinate swap (IFC Z-up → Y-up) is already baked in on the server.
function buildMeshFromGlb(glbBase64, color) {
  return new Promise((resolve) => {
    const binary = Uint8Array.from(atob(glbBase64), c => c.charCodeAt(0));
    _gltfLoader.parse(binary.buffer, '', (gltf) => {
      let geo = null;
      gltf.scene.traverse(obj => { if (obj.isMesh && !geo) geo = obj.geometry; });
      if (!geo) { resolve(null); return; }
      resolve(new THREE.Mesh(geo, new THREE.MeshPhongMaterial({
        color, side: THREE.DoubleSide, shininess: 40,
      })));
    }, (err) => { console.error("GLTFLoader error:", err); resolve(null); });
  });
}

function clearScene() {
  for (const m of sceneMeshes) {
    scene.remove(m);
    m.geometry.dispose();
    m.material.dispose();
  }
  sceneMeshes = [];
  meshByGuid   = {};
  selectedGuid = null;
}

function selectElement(guid) {
  if (selectedGuid === guid) {
    selectedGuid = null;
    for (const m of Object.values(meshByGuid)) {
      m.material.emissive.setHex(0x000000);
      m.material.opacity    = 1;
      m.material.transparent = false;
    }
  } else {
    selectedGuid = guid;
    for (const [g, m] of Object.entries(meshByGuid)) {
      if (g === guid) {
        m.material.emissive.setHex(0x777777);
        m.material.opacity    = 1;
        m.material.transparent = false;
      } else {
        m.material.emissive.setHex(0x000000);
        m.material.opacity    = 0.15;
        m.material.transparent = true;
      }
    }
  }
  document.querySelectorAll("#element-list .el-row").forEach(r => {
    r.classList.toggle("selected", r.dataset.guid === selectedGuid);
  });
}

// ─── GraphQL fetch ───────────────────────────────────────────────────────────

function endpoint() {
  return document.getElementById("endpoint").value.trim().replace(/\/$/, "");
}

async function gqlFetch(query, variables = {}) {
  const res = await fetch(endpoint() + "/graphql", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, variables }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const json = await res.json();
  if (json.errors?.length) throw new Error(json.errors[0].message);
  return json.data;
}

// ─── Queries ─────────────────────────────────────────────────────────────────

const TOPIC_QUERY = `
query($guid: ID!) {
  topic(guid: $guid) {
    guid title
    creationDate { ISO8601 }
    files {
      ifcProjectGuid
      fileName
    }
    viewpoints {
      camera {
        ... on PerspectiveCamera {
          cameraViewPoint { x y z }
          cameraDirection { x y z }
          fieldOfView
        }
        ... on OrthogonalCamera {
          cameraViewPoint { x y z }
          cameraDirection { x y z }
          viewToWorldScale
        }
      }
      components {
        selection { ifcGuid }
        coloring { color components { ifcGuid } }
      }
    }
  }
}`;

const VERSIONS_QUERY = `
query {
  ifcVersions {
    id
    fileName
    exportedAt
  }
}`;

const TIMELINE_QUERY = `
query($guid: ID!) {
  topicTimeline(topicGuid: $guid) {
    eventType
    timestamp { ISO8601 }
    author
    detail
    ifcVersion {
      version { fileName }
      inferred
    }
  }
}`;

// Query the latest version of each element (no version arg = newest file containing it).
function buildLastModifiedQuery(guids) {
  const aliases = guids.map((g, i) => `
    e${i}: ifcElement(globalId: "${g}") { globalId fileName version }
  `).join("\n");
  return `query { ${aliases} }`;
}

// Build a batched query: one alias per selected GUID at a given version number.
function buildGeometryQuery(guids, versionNum) {
  const aliases = guids.map((g, i) => `
    e${i}: ifcElement(globalId: "${g}", version: ${versionNum}) {
      globalId name type
      geometry { glb }
    }
  `).join("\n");
  return `query { ${aliases} }`;
}

// ─── Version matching ────────────────────────────────────────────────────────
// Mirrors the server-side 4-tier match_version logic so the viewer starts on
// the IFC version the topic was actually created against, not just the latest.

function findVersionForTopic(topic, versions) {
  if (!versions.length) return 0;
  const files     = topic.files || [];
  const eventTime = topic.creationDate?.ISO8601 || null;

  // Return the index of the latest matching version exported before eventTime.
  // When the IFC file post-dates the BCF event (common when files are loaded
  // after import), fall back to the earliest overall match — file identity is
  // still known from the GUID or filename.
  function bestMatch(pred) {
    for (let i = versions.length - 1; i >= 0; i--) {
      if ((!eventTime || versions[i].exportedAt <= eventTime) && pred(versions[i])) {
        return i;
      }
    }
    for (let i = 0; i < versions.length; i++) {
      if (pred(versions[i])) return i;
    }
    return -1;
  }

  // Tier 1 — exact file reference: GUID + filename from the SAME BCF header entry.
  // Prevents picking a newer version of the same project (same GUID, different
  // filename) when the BCF header explicitly names an older file.
  for (const f of files) {
    if (!f.ifcProjectGuid || !f.fileName) continue;
    const nb  = f.fileName.split(/[/\\]/).pop().toLowerCase();
    const idx = bestMatch(v =>
      v.id === f.ifcProjectGuid &&
      v.fileName.split(/[/\\]/).pop().toLowerCase() === nb
    );
    if (idx >= 0) return idx;
  }

  // Tier 2 — GUID only (stable across file renames)
  for (const f of files) {
    if (!f.ifcProjectGuid) continue;
    const idx = bestMatch(v => v.id === f.ifcProjectGuid);
    if (idx >= 0) return idx;
  }

  // Tier 3 — filename only (case-insensitive basename; BCF tools often store full paths)
  for (const f of files) {
    if (!f.fileName) continue;
    const nb  = f.fileName.split(/[/\\]/).pop().toLowerCase();
    const idx = bestMatch(v => v.fileName.split(/[/\\]/).pop().toLowerCase() === nb);
    if (idx >= 0) return idx;
  }

  // Tier 4 — no BCF file reference: start at the latest version
  return versions.length - 1;
}

// ─── Load topic (step 1) ─────────────────────────────────────────────────────

async function loadTopic() {
  const topicGuid = document.getElementById("topic-guid").value.trim();
  if (!topicGuid) { setStatus("Please enter a topic GUID.", "error"); return; }

  document.getElementById("load-btn").disabled = true;
  setStatus("Loading topic…");
  clearScene();
  selectedComponents = [];
  allVersions = [];
  currentVersionIdx = -1;
  bcfCamera = null;
  lastModifiedMap  = {};
  document.getElementById("version-box").style.display = "none";
  document.getElementById("match-info").style.display = "none";
  document.getElementById("diff-box").style.display = "none";
  document.getElementById("diff-divider").style.display = "none";
  document.getElementById("element-list").innerHTML = "";
  document.getElementById("legend").innerHTML = "";
  document.getElementById("timeline-list").innerHTML = "";
  currentElementNames = {};

  try {
    // Fetch topic, versions, and BCF timeline in parallel
    const [topicData, versionsData, timelineData] = await Promise.all([
      gqlFetch(TOPIC_QUERY, { guid: topicGuid }),
      gqlFetch(VERSIONS_QUERY),
      gqlFetch(TIMELINE_QUERY, { guid: topicGuid }),
    ]);

    const topic = topicData?.topic;
    if (!topic) { setStatus("Topic not found.", "error"); return; }

    allVersions = versionsData?.ifcVersions || [];
    if (allVersions.length === 0) {
      setStatus("No IFC files found in ifcs/.", "error"); return;
    }

    // Pick the viewpoint that has selected components
    const vp = topic.viewpoints?.find(v => v.components?.selection?.length > 0)
             || topic.viewpoints?.[0];

    if (!vp?.components?.selection?.length) {
      setStatus("No selected components in viewpoint.", "error"); return;
    }

    bcfCamera = vp.camera || null;

    // Build guid → BCF color map
    const guidColorMap = {};
    for (const cg of vp.components?.coloring || []) {
      const hex = bcfColorToHex(cg.color);
      if (hex == null) continue;
      for (const c of cg.components || []) {
        if (c.ifcGuid) guidColorMap[c.ifcGuid] = hex;
      }
    }

    const DEFAULT_COLOR = 0x6366f1;
    selectedComponents = vp.components.selection
      .filter(c => c.ifcGuid)
      .map(c => ({ ifcGuid: c.ifcGuid, color: guidColorMap[c.ifcGuid] ?? DEFAULT_COLOR }));

    // Find the latest version each element appears in (last-modified info)
    const lmGuids = selectedComponents.map(c => c.ifcGuid);
    const lmData  = await gqlFetch(buildLastModifiedQuery(lmGuids));
    lastModifiedMap = {};
    for (let i = 0; i < lmGuids.length; i++) {
      const el = lmData?.[`e${i}`];
      if (el?.globalId) lastModifiedMap[el.globalId] = { fileName: el.fileName, version: el.version };
    }

    currentVersionIdx = findVersionForTopic(topic, allVersions);
    document.getElementById("version-box").style.display = "flex";
    await renderCurrentVersion();
    renderTimeline(timelineData?.topicTimeline || []);

    setStatus(`Loaded "${topic.title}"`, "ok");
  } catch (e) {
    setStatus("Error: " + e.message, "error");
    console.error(e);
  }

  document.getElementById("load-btn").disabled = false;
}

// ─── Render geometry at currentVersionIdx (step 2) ───────────────────────────

async function renderCurrentVersion() {
  if (selectedComponents.length === 0 || allVersions.length === 0) return;

  const versionNum = currentVersionIdx + 1; // API is 1-indexed
  const vInfo      = allVersions[currentVersionIdx];

  updateVersionUI(vInfo, versionNum);
  setStatus("Fetching geometry…");
  clearScene();
  document.getElementById("element-list").innerHTML = "";
  document.getElementById("legend").innerHTML = "";
  document.getElementById("version-missing").style.display = "none";

  const guids = selectedComponents.map(c => c.ifcGuid);
  const data  = await gqlFetch(buildGeometryQuery(guids, versionNum));

  currentElementNames = {};
  let rendered = 0;
  let missing  = 0;
  const elListEl  = document.getElementById("element-list");
  const legendMap = {};

  for (let i = 0; i < selectedComponents.length; i++) {
    const { ifcGuid, color } = selectedComponents[i];
    const el = data[`e${i}`];

    const row = document.createElement("div");
    row.className = "el-row";
    row.style.borderLeftColor = "#" + color.toString(16).padStart(6, "0");

    if (!el) {
      row.classList.add("missing");
      row.innerHTML = `<div>${ifcGuid.slice(0, 12)}…</div><div class="el-type">not in this version</div>`;
      elListEl.appendChild(row);
      missing++;
      continue;
    }

    if (el.globalId) currentElementNames[el.globalId] = { name: el.name, type: el.type };

    const lm = lastModifiedMap[ifcGuid];
    const lmLine = lm?.fileName ? `<div class="el-lm">${lm.fileName}</div>` : "";
    row.innerHTML = `<div>${el.name || el.globalId}</div><div class="el-type">${el.type || ""}</div>${lmLine}`;
    elListEl.appendChild(row);

    if (!el.geometry?.glb) {
      legendMap[0x888888] = "No geometry";
      continue;
    }

    const mesh = await buildMeshFromGlb(el.geometry.glb, color);
    if (!mesh) continue;

    scene.add(mesh);
    sceneMeshes.push(mesh);
    meshByGuid[ifcGuid] = mesh;
    rendered++;
    row.dataset.guid = ifcGuid;
    row.addEventListener("click", () => { selectElement(ifcGuid); focusMesh(mesh); });
    legendMap[color] = el.type || "Element";
  }

  // Legend
  const legendEl = document.getElementById("legend");
  for (const [hex, label] of Object.entries(legendMap)) {
    const item = document.createElement("div");
    item.className = "legend-item";
    item.innerHTML = `
      <div class="swatch" style="background:#${Number(hex).toString(16).padStart(6,"0")}"></div>
      <span>${label}</span>`;
    legendEl.appendChild(item);
  }

  if (missing > 0) {
    document.getElementById("version-missing").style.display = "block";
  }

  // Camera: always re-apply BCF camera (it doesn't change between versions)
  if (bcfCamera && rendered > 0) {
    applyCameraFromBCF(bcfCamera);
  } else if (rendered > 0) {
    frameMeshes();
  }

  const statusMsg = rendered > 0
    ? `v${versionNum} — ${rendered} element(s) rendered`
    : `v${versionNum} — no geometry in this version`;
  setStatus(statusMsg, rendered > 0 ? "ok" : "");

  await fetchAndRenderDiff();
}

// ─── Version UI ──────────────────────────────────────────────────────────────

function updateVersionUI(vInfo, versionNum) {
  const total     = allVersions.length;
  const date      = vInfo.exportedAt
    ? new Date(vInfo.exportedAt).toLocaleDateString(undefined, { year:"numeric", month:"short", day:"numeric" })
    : "";
  const shortGuid = vInfo.id ? vInfo.id.slice(0, 13) + "…" : "—";

  document.getElementById("version-label").innerHTML = `
    <div class="vnum">Version ${versionNum} / ${total}</div>
    <div class="vfile">${vInfo.fileName}</div>
    <div class="vguid">${shortGuid}</div>
    <div class="vdate">${date}</div>`;

  document.getElementById("prev-btn").disabled = currentVersionIdx <= 0;
  document.getElementById("next-btn").disabled = currentVersionIdx >= allVersions.length - 1;

  const matchInfoEl = document.getElementById("match-info");
  const lmFiles = [...new Set(
    Object.values(lastModifiedMap).map(e => e.fileName).filter(Boolean)
  )];

  if (lmFiles.length > 0) {
    matchInfoEl.style.display = "flex";
    matchInfoEl.innerHTML = `
      <div class="mi-head">
        <span class="mi-title">Last Modified In</span>
      </div>
      ${lmFiles.map(f => `<div class="mi-row"><span class="mi-val">${f}</span></div>`).join("")}`;
  } else {
    matchInfoEl.style.display = "none";
  }
}

async function goToPrevVersion() {
  if (currentVersionIdx <= 0) return;
  currentVersionIdx--;
  await renderCurrentVersion();
}

async function goToNextVersion() {
  if (currentVersionIdx >= allVersions.length - 1) return;
  currentVersionIdx++;
  await renderCurrentVersion();
}

// ─── Camera helpers ───────────────────────────────────────────────────────────

function applyCameraFromBCF(cam) {
  const vp  = cam.cameraViewPoint;
  const dir = cam.cameraDirection;
  if (!vp || !dir) return;
  camera.position.set(vp.x, vp.z, -vp.y);
  controls.target.set(vp.x + dir.x, vp.z + dir.z, -vp.y - dir.y);
  if (cam.fieldOfView) camera.fov = cam.fieldOfView;
  camera.updateProjectionMatrix();
  controls.update();
}

function frameMeshes() {
  if (!sceneMeshes.length) return;
  const box    = new THREE.Box3();
  for (const m of sceneMeshes) box.expandByObject(m);
  const center = box.getCenter(new THREE.Vector3());
  const size   = box.getSize(new THREE.Vector3()).length();
  camera.position.copy(center).addScaledVector(new THREE.Vector3(1, 1, 2).normalize(), size * 1.5);
  controls.target.copy(center);
  controls.update();
}

function focusMesh(mesh) {
  const box    = new THREE.Box3().setFromObject(mesh);
  const center = box.getCenter(new THREE.Vector3());
  const size   = box.getSize(new THREE.Vector3()).length();
  camera.position.copy(center).addScaledVector(new THREE.Vector3(1, 1, 2).normalize(), size * 1.5);
  controls.target.copy(center);
  controls.update();
}

// ─── BCF Timeline ────────────────────────────────────────────────────────────

const RESOLVED_STATUSES = new Set(["resolved", "closed", "done", "completed", "fixed"]);

function getEventColor(ev) {
  if (ev.eventType === "CREATION")   return "#ef5350";
  if (ev.eventType === "MODIFICATION") return "#ffa726";
  if (ev.eventType === "COMMENT")    return "#64b5f6";
  if (ev.eventType === "STATUS_CHANGE") {
    const detail = (ev.detail || "").toLowerCase().trim();
    return RESOLVED_STATUSES.has(detail) ? "#4caf50" : "#ffa726";
  }
  return "#888";
}

function renderTimeline(events) {
  const container = document.getElementById("timeline-list");
  container.innerHTML = "";
  if (!events?.length) return;

  const sorted = [...events].sort((a, b) => {
    const ta = a.timestamp?.ISO8601 ?? "";
    const tb = b.timestamp?.ISO8601 ?? "";
    return ta < tb ? -1 : ta > tb ? 1 : 0;
  });

  // ── Group by calendar date ────────────────────────────────────────────────
  const groupMap = new Map();
  sorted.forEach(ev => {
    const iso = ev.timestamp?.ISO8601;
    const key = iso ? iso.slice(0, 10) : "__none__";
    if (!groupMap.has(key)) {
      groupMap.set(key, {
        dateStr: iso
          ? new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short" })
          : "—",
        events: []
      });
    }
    groupMap.get(key).events.push(ev);
  });
  const groups = [...groupMap.values()];

  // ── Axis visualization (HTML, not SVG) ────────────────────────────────────
  const wrap = document.createElement("div");
  wrap.className = "tl-wrap";

  // Top row: date label + circle marker per column, with axis line as bottom border
  const axisRow = document.createElement("div");
  axisRow.className = "tl-axis-row";
  groups.forEach(g => {
    const col = document.createElement("div");
    col.className = "tl-axis-col";
    col.innerHTML = `<div class="tl-date">${g.dateStr}</div><div class="tl-marker"></div>`;
    axisRow.appendChild(col);
  });
  const arrow = document.createElement("div");
  arrow.className = "tl-arrow";
  arrow.textContent = "▶";
  axisRow.appendChild(arrow);

  // Body row: one column per date, each with a vertical stem + events
  const bodyRow = document.createElement("div");
  bodyRow.className = "tl-body-row";
  groups.forEach(g => {
    const col = document.createElement("div");
    col.className = "tl-ev-col";
    g.events.forEach(ev => {
      const color = getEventColor(ev);
      const label = ev.eventType.replace(/_/g, " ").toLowerCase();
      const item = document.createElement("div");
      item.className = "tl-ev-item";
      item.innerHTML = `
        <span class="tl-dot" style="background:${color}"></span>
        <span class="tl-label" style="color:${color}">${label}</span>`;
      col.appendChild(item);
    });
    bodyRow.appendChild(col);
  });

  wrap.appendChild(axisRow);
  wrap.appendChild(bodyRow);
  container.appendChild(wrap);

  // ── Detail cards ──────────────────────────────────────────────────────────
  sorted.forEach(ev => {
    const color  = getEventColor(ev);
    const label  = ev.eventType.replace(/_/g, " ");
    const date   = ev.timestamp?.ISO8601
      ? new Date(ev.timestamp.ISO8601).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" })
      : "—";
    const ifcFile = ev.ifcVersion?.version?.fileName;
    const ifcLine = ifcFile
      ? `<div class="ev-ifc">${ev.ifcVersion.inferred ? "~" : ""}${ifcFile}</div>`
      : "";
    const row = document.createElement("div");
    row.className = "ev-row";
    row.style.borderLeftColor = color;
    row.innerHTML = `
      <div class="ev-header">
        <span class="ev-type" style="color:${color}">${label}</span>
        <span class="ev-date">${date}</span>
      </div>
      <div class="ev-author">${ev.author || "—"}</div>
      ${ev.detail ? `<div class="ev-detail">${ev.detail}</div>` : ""}
      ${ifcLine}`;
    container.appendChild(row);
  });
}


// ─── Version Diff ────────────────────────────────────────────────────────────

let currentElementNames = {}; // { ifcGuid: { name, type } }

function buildDiffQuery(guids, projectGuid, ifcNameA, ifcNameB) {
  const esc = s => s.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  const aliases = guids.map((g, i) => `
    d${i}: ifcElementDiff(
      ifcProjectGuid: "${esc(projectGuid)}"
      globalId: "${esc(g)}"
      ifcNameA: "${esc(ifcNameA)}"
      ifcNameB: "${esc(ifcNameB)}"
    ) {
      globalId status unchanged
      attributeChanges { attribute oldValue newValue }
      propertyChanges { pset property oldValue newValue }
      geometryChanged
      typeChanged oldType newType
      containerChanged oldContainer newContainer
      aggregateChanged classificationChanged
    }
  `).join("\n");
  return `query { ${aliases} }`;
}

function renderDiffResults(data, guids) {
  const container = document.getElementById("diff-content");
  container.innerHTML = "";

  for (let i = 0; i < guids.length; i++) {
    const guid = guids[i];
    const diff = data?.[`d${i}`];
    const comp = selectedComponents[i];
    const colorHex = "#" + comp.color.toString(16).padStart(6, "0");
    const displayName = currentElementNames[guid]?.name || guid.slice(0, 12) + "…";

    const el = document.createElement("div");
    el.className = "diff-el";
    el.style.borderLeftColor = colorHex;

    if (!diff) {
      el.innerHTML = `
        <div class="diff-el-header">
          <span class="diff-el-name" title="${guid}">${displayName}</span>
          <span class="status-badge unchanged">N/A</span>
        </div>`;
      container.appendChild(el);
      continue;
    }

    const status = (diff.status || "unchanged").toLowerCase();
    let bodyHtml = "";

    if (status === "added") {
      bodyHtml = `<div class="diff-flag">New in this version</div>`;
    } else if (status === "deleted") {
      bodyHtml = `<div class="diff-flag">Removed in this version</div>`;
    } else if (status === "modified" && !diff.unchanged) {
      for (const ac of diff.attributeChanges || []) {
        bodyHtml += `
          <div class="diff-change">
            <span class="diff-change-label">${ac.attribute}</span>
            <div class="diff-val-row">
              <span class="diff-old">${ac.oldValue ?? "—"}</span>
              <span class="diff-arrow">→</span>
              <span class="diff-new">${ac.newValue ?? "—"}</span>
            </div>
          </div>`;
      }
      for (const pc of diff.propertyChanges || []) {
        bodyHtml += `
          <div class="diff-change">
            <span class="diff-change-label">${pc.pset} · ${pc.property}</span>
            <div class="diff-val-row">
              <span class="diff-old">${pc.oldValue ?? "—"}</span>
              <span class="diff-arrow">→</span>
              <span class="diff-new">${pc.newValue ?? "—"}</span>
            </div>
          </div>`;
      }
      if (diff.geometryChanged)       bodyHtml += `<div class="diff-flag">⬡ Geometry changed</div>`;
      if (diff.typeChanged)           bodyHtml += `
          <div class="diff-change">
            <span class="diff-change-label">Type</span>
            <div class="diff-val-row">
              <span class="diff-old">${diff.oldType ?? "—"}</span>
              <span class="diff-arrow">→</span>
              <span class="diff-new">${diff.newType ?? "—"}</span>
            </div>
          </div>`;
      if (diff.containerChanged)      bodyHtml += `
          <div class="diff-change">
            <span class="diff-change-label">Container</span>
            <div class="diff-val-row">
              <span class="diff-old">${diff.oldContainer ?? "—"}</span>
              <span class="diff-arrow">→</span>
              <span class="diff-new">${diff.newContainer ?? "—"}</span>
            </div>
          </div>`;
      if (diff.aggregateChanged)      bodyHtml += `<div class="diff-flag">⬡ Aggregate changed</div>`;
      if (diff.classificationChanged) bodyHtml += `<div class="diff-flag">⬡ Classification changed</div>`;
      if (!bodyHtml) bodyHtml = `<div class="diff-no-change">Modified (no tracked property changes)</div>`;
    } else {
      bodyHtml = `<div class="diff-no-change">Unchanged</div>`;
    }

    el.innerHTML = `
      <div class="diff-el-header">
        <span class="diff-el-name" title="${guid}">${displayName}</span>
        <span class="status-badge ${status}">${diff.status}</span>
      </div>
      <div class="diff-el-body">${bodyHtml}</div>`;

    container.appendChild(el);
  }
}

async function fetchAndRenderDiff() {
  const diffBox    = document.getElementById("diff-box");
  const diffDivider = document.getElementById("diff-divider");

  if (currentVersionIdx <= 0 || selectedComponents.length === 0) {
    diffBox.style.display = "none";
    diffDivider.style.display = "none";
    return;
  }

  const currentVer   = allVersions[currentVersionIdx];
  const projectGuid  = currentVer.id;
  const projectVers  = allVersions.filter(v => v.id === projectGuid);
  const idxInProject = projectVers.findIndex(v => v.fileName === currentVer.fileName);

  if (idxInProject <= 0) {
    diffBox.style.display = "none";
    diffDivider.style.display = "none";
    return;
  }

  const prevVer  = projectVers[idxInProject - 1];
  const ifcNameA = prevVer.fileName;
  const ifcNameB = currentVer.fileName;

  diffBox.style.display = "flex";
  diffDivider.style.display = "block";
  document.getElementById("diff-comparing").textContent = `${ifcNameA}  →  ${ifcNameB}`;
  document.getElementById("diff-content").innerHTML =
    '<div style="color:#888;font-size:10px;padding:2px 0;">Computing diff…</div>';

  try {
    const guids = selectedComponents.map(c => c.ifcGuid);
    const data  = await gqlFetch(buildDiffQuery(guids, projectGuid, ifcNameA, ifcNameB));
    renderDiffResults(data, guids);
  } catch (e) {
    document.getElementById("diff-content").innerHTML =
      `<div style="color:#e94560;font-size:10px;">${e.message}</div>`;
  }
}

// ─── Wire up ─────────────────────────────────────────────────────────────────

document.getElementById("load-btn").addEventListener("click", loadTopic);
document.getElementById("prev-btn").addEventListener("click", goToPrevVersion);
document.getElementById("next-btn").addEventListener("click", goToNextVersion);

const params = new URLSearchParams(window.location.search);
if (params.get("topic")) {
  document.getElementById("topic-guid").value = params.get("topic");
  loadTopic();
}