import * as THREE from 'https://esm.sh/three@0.160.0';
import { OrbitControls } from 'https://esm.sh/three@0.160.0/examples/jsm/controls/OrbitControls.js';
import * as WebIFC from 'https://esm.sh/web-ifc@0.0.57';

const GQL = '/graphql';

const viewportEl    = document.getElementById('viewport');
const loaderOverlay = document.getElementById('loader-overlay');
const loaderTextEl  = document.getElementById('loader-text');
const statusBar     = document.getElementById('status-bar');
const elementStrip  = document.getElementById('element-strip');
const elementGuidEl = document.getElementById('element-guid-display');
const topicSection  = document.getElementById('topic-section');

function showLoader(msg) { loaderTextEl.textContent = msg; loaderOverlay.classList.add('visible'); }
function hideLoader()    { loaderOverlay.classList.remove('visible'); }
function setStatus(msg, cls = '') { statusBar.textContent = msg; statusBar.className = cls; }

// ── Three.js scene ────────────────────────────────────────────────────────────
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(window.devicePixelRatio);
renderer.shadowMap.enabled = false;
viewportEl.appendChild(renderer.domElement);

const scene  = new THREE.Scene();
scene.background = new THREE.Color(0x060a12);

const camera = new THREE.PerspectiveCamera(45, 1, 0.01, 5000);
camera.position.set(20, 15, 20);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.08;

scene.add(new THREE.AmbientLight(0xffffff, 0.7));
const sun = new THREE.DirectionalLight(0xffffff, 0.9);
sun.position.set(1, 2, 1.5);
scene.add(sun);

function resize() {
  const w = viewportEl.clientWidth;
  const h = viewportEl.clientHeight;
  renderer.setSize(w, h);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
resize();
window.addEventListener('resize', resize);

(function animate() {
  requestAnimationFrame(animate);
  controls.update();
  renderer.render(scene, camera);
})();

// ── web-ifc init ──────────────────────────────────────────────────────────────
const api = new WebIFC.IfcAPI();
// Point to unpkg CDN for the .wasm binary
api.SetWasmPath('https://unpkg.com/web-ifc@0.0.57/', true);
await api.Init();

// ── Click → GlobalId ──────────────────────────────────────────────────────────
const raycaster = new THREE.Raycaster();
const mouse     = new THREE.Vector2();
const clickable = [];   // all meshes that can be picked
let   highlightedExpressId = null;

const HIGHLIGHT_COLOR = new THREE.Color(0x6366f1);
const ORIG_COLOR_KEY  = '__origColor';

function clearHighlight() {
  if (highlightedExpressId === null) return;
  for (const m of clickable) {
    if (m.userData.expressId === highlightedExpressId && m.material[ORIG_COLOR_KEY]) {
      m.material.color.copy(m.material[ORIG_COLOR_KEY]);
    }
  }
  highlightedExpressId = null;
}

function applyHighlight(expressId) {
  for (const m of clickable) {
    if (m.userData.expressId === expressId) {
      if (!m.material[ORIG_COLOR_KEY]) {
        m.material[ORIG_COLOR_KEY] = m.material.color.clone();
      }
      m.material.color.copy(HIGHLIGHT_COLOR);
    }
  }
  highlightedExpressId = expressId;
}

viewportEl.addEventListener('click', async (evt) => {
  const rect = viewportEl.getBoundingClientRect();
  mouse.x =  ((evt.clientX - rect.left) / rect.width)  * 2 - 1;
  mouse.y = -((evt.clientY - rect.top)  / rect.height) * 2 + 1;

  raycaster.setFromCamera(mouse, camera);
  const hits = raycaster.intersectObjects(clickable, false);
  if (!hits.length) return;

  const mesh = hits[0].object;
  const { expressId, modelId } = mesh.userData;
  if (expressId == null) return;

  // Highlight all meshes belonging to the clicked element
  if (highlightedExpressId !== expressId) {
    clearHighlight();
    applyHighlight(expressId);
  }

  // Get GlobalId from IFC properties
  let globalId = null;
  try {
    const props = api.GetLine(modelId, expressId, false);
    globalId = props?.GlobalId?.value ?? null;
  } catch (e) {
    console.warn('GetLine failed for expressId', expressId, e);
  }

  if (!globalId) {
    setStatus('Element has no GlobalId.', 'error');
    return;
  }

  elementGuidEl.textContent = globalId;
  elementStrip.classList.add('visible');
  setStatus('Selected: ' + globalId, 'selected');
  await fetchAndRenderTopics(globalId);
});

// ── File picker ───────────────────────────────────────────────────────────────
let currentModelId        = null;
let currentGroup          = null;
let currentFileName       = null;
let currentIfcProjectGuid = null;

document.getElementById('file-input').addEventListener('change', async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  e.target.value = '';

  // Tear down previous model
  if (currentModelId !== null) {
    try { api.CloseModel(currentModelId); } catch (_) {}
    currentModelId = null;
  }
  if (currentGroup) {
    scene.remove(currentGroup);
    currentGroup.traverse(o => {
      if (o.isMesh) { o.geometry.dispose(); o.material.dispose(); }
    });
    currentGroup = null;
  }
  clickable.length = 0;
  highlightedExpressId = null;
  elementStrip.classList.remove('visible');
  topicSection.innerHTML = '<div class="empty-state">Click an element to see its BCF topics.</div>';

  showLoader(`Parsing ${file.name}…`);
  setStatus(`Loading ${file.name}…`);

  try {
    const buffer   = new Uint8Array(await file.arrayBuffer());
    const modelId  = api.OpenModel(buffer, { COORDINATE_TO_ORIGIN: true, USE_FAST_BOOLS: false });
    currentModelId  = modelId;
    currentFileName = file.name;

    // Extract IfcProject GlobalId so we can scope the topicsForElement query
    currentIfcProjectGuid = null;
    try {
      const projects = api.GetLineIDsWithType(modelId, WebIFC.IFCPROJECT);
      if (projects.size() > 0) {
        const proj = api.GetLine(modelId, projects.get(0), false);
        currentIfcProjectGuid = proj?.GlobalId?.value ?? null;
      }
    } catch (_) {}

    const group = new THREE.Group();

    // StreamAllMeshes visits every IFC element that has geometry
    api.StreamAllMeshes(modelId, (ifcMesh) => {
      const expressId   = ifcMesh.expressID;
      const placedGeoms = ifcMesh.geometries;

      for (let i = 0; i < placedGeoms.size(); i++) {
        const placed   = placedGeoms.get(i);
        const geomData = api.GetGeometry(modelId, placed.geometryExpressID);

        // web-ifc vertex buffer: interleaved [x,y,z, nx,ny,nz, ...] (6 floats/vertex)
        const verts = api.GetVertexArray(
          geomData.GetVertexData(),
          geomData.GetVertexDataSize(),
        ).slice();   // .slice() copies out of WASM memory before it may be freed

        const indices = api.GetIndexArray(
          geomData.GetIndexData(),
          geomData.GetIndexDataSize(),
        ).slice();

        geomData.delete();

        const buf = new THREE.InterleavedBuffer(verts, 6);
        const geo = new THREE.BufferGeometry();
        geo.setAttribute('position', new THREE.InterleavedBufferAttribute(buf, 3, 0));
        geo.setAttribute('normal',   new THREE.InterleavedBufferAttribute(buf, 3, 3));
        geo.setIndex(new THREE.BufferAttribute(indices, 1));

        // Apply placement transform
        geo.applyMatrix4(new THREE.Matrix4().fromArray(placed.flatTransformation));

        const c   = placed.color;
        const mat = new THREE.MeshPhongMaterial({
          color:       new THREE.Color(c.x, c.y, c.z),
          opacity:     c.w,
          transparent: c.w < 1,
          side:        THREE.DoubleSide,
        });

        const mesh = new THREE.Mesh(geo, mat);
        mesh.userData.expressId = expressId;
        mesh.userData.modelId   = modelId;

        group.add(mesh);
        clickable.push(mesh);
      }
    });

    currentGroup = group;
    scene.add(group);

    // Fit camera to model bounding box
    const box    = new THREE.Box3().setFromObject(group);
    const center = box.getCenter(new THREE.Vector3());
    const size   = box.getSize(new THREE.Vector3());
    const dist   = Math.max(size.x, size.y, size.z) * 1.6;

    camera.position.set(center.x + dist, center.y + dist * 0.6, center.z + dist);
    controls.target.copy(center);
    controls.update();

    setStatus(`${file.name} — ${clickable.length} elements. Click one.`, 'active');
  } catch (err) {
    setStatus('Load error: ' + err.message, 'error');
    console.error('IFC load error:', err);
  } finally {
    hideLoader();
  }
});

// ── GraphQL ───────────────────────────────────────────────────────────────────
async function fetchAndRenderTopics(globalId) {
  topicSection.innerHTML = `
    <div class="topics-loading">
      <div class="mini-spinner"></div>Fetching BCF topics…
    </div>`;
  try {
    // 1 — fetch all topic versions for this element
    const res = await fetch(GQL, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        query: `query($globalId: ID!, $ifcProjectGuid: ID) {
          topicsForElement(
            globalId:       $globalId
            ifcProjectGuid: $ifcProjectGuid
            includeHistory: true
          ) {
            guid
            version
            title
            topicStatus
            topicType
            assignedTo
            description
            files {
              ifcProjectGuid
              fileName
            }
            comments {
              guid
              date { ISO8601 }
              author
              comment
            }
          }
        }`,
        variables: {
          globalId,
          ifcProjectGuid: currentIfcProjectGuid,
        },
      }),
    });
    const json = await res.json();
    if (json.errors?.length) {
      topicSection.innerHTML = `<div class="empty-state">GraphQL error:<br>${esc(json.errors[0].message)}</div>`;
      return;
    }

    const topics = json.data?.topicsForElement ?? [];

    // 2 — for each topic, ask the backend which IFC version it belongs to
    //     using the existing ifcVersionForEvent query (runs in parallel)
    const versionMatches = await Promise.all(
      topics.map(t => fetchVersionForTopic(t.guid))
    );

    renderTopics(topics, versionMatches);
  } catch (err) {
    topicSection.innerHTML = `<div class="empty-state">Fetch error:<br>${esc(err.message)}</div>`;
  }
}

async function fetchVersionForTopic(topicGuid) {
  try {
    const res = await fetch(GQL, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        query: `query($topicGuid: ID!) {
          ifcVersionForEvent(topicGuid: $topicGuid) {
            inferred
            version {
              id
              fileName
              exportedAt
            }
          }
        }`,
        variables: { topicGuid },
      }),
    });
    const json = await res.json();
    return json.data?.ifcVersionForEvent ?? null;
  } catch (_) {
    return null;
  }
}

function renderTopics(topics, versionMatches = []) {
  if (!topics.length) {
    topicSection.innerHTML = '<div class="empty-state">No BCF topics linked<br>to this element.</div>';
    return;
  }
  const header = `<div class="section-label">${topics.length} BCF Topic${topics.length !== 1 ? 's' : ''}</div>`;
  const cards  = topics.map((t, i) => {
    const vm          = versionMatches[i] ?? null;
    const fileName    = vm?.version?.fileName ?? null;
    const versionGuid = vm?.version?.id ?? null;
    const inferred    = vm?.inferred ?? false;

    // Determine whether this topic references the currently loaded IFC file.
    // Priority 1: check the topic's own BCF file references (explicit header data).
    //   A file reference matches when every field it provides agrees with the loaded file.
    //   GUID takes precedence — if the ref has a GUID it must match; same for fileName.
    // Priority 2: fall back to the server's 3-tier matched version (GUID then filename).
    const files = t.files ?? [];

    function bcfFileMatchesCurrent(f) {
      if (!f.ifcProjectGuid && !f.fileName) return false;
      if (f.ifcProjectGuid && currentIfcProjectGuid && f.ifcProjectGuid !== currentIfcProjectGuid) return false;
      if (f.fileName && currentFileName && f.fileName !== currentFileName) return false;
      return true;
    }

    let isCurrent;
    if (files.length > 0) {
      isCurrent = files.some(bcfFileMatchesCurrent);
    } else if (versionGuid && currentIfcProjectGuid) {
      isCurrent = versionGuid === currentIfcProjectGuid;
    } else {
      isCurrent = !!fileName && fileName === currentFileName;
    }

    let versionBadge = '';
    if (isCurrent) {
      versionBadge = `<span class="badge current-file">✓ Current file</span>`;
    } else if (fileName) {
      const label = inferred ? `~${esc(fileName)}` : esc(fileName);
      versionBadge = `<span class="badge other-file" title="${esc(fileName)}">${label}</span>`;
    }

    const commentsHtml = t.comments?.length
      ? `<div class="topic-comments">
           ${t.comments.map(c => `
             <div class="comment-item">
               <div class="comment-meta">${esc(c.author)} · ${fmtDate(c.date)}</div>
               <div class="comment-text">${esc(c.comment ?? '')}</div>
             </div>`).join('')}
         </div>`
      : '';

    return `
      <div class="topic-card${isCurrent ? ' is-current' : ''}">
        <div class="version-row">
          <span class="version-num">v${t.version ?? '?'}</span>
          ${versionBadge}
        </div>
        <div class="topic-title">${esc(t.title)}</div>
        <div class="topic-badges">
          ${t.topicStatus ? `<span class="badge ${statusCls(t.topicStatus)}">${esc(t.topicStatus)}</span>` : ''}
          ${t.topicType   ? `<span class="badge">${esc(t.topicType)}</span>` : ''}
          ${t.assignedTo  ? `<span class="badge">→ ${esc(t.assignedTo)}</span>` : ''}
        </div>
        ${t.description ? `<div class="topic-description">${esc(t.description)}</div>` : ''}
        ${commentsHtml}
        <div class="topic-guid">${esc(t.guid)}</div>
      </div>`;
  }).join('');
  topicSection.innerHTML = header + cards;
}

function fmtDate(date) {
  const iso = date?.ISO8601 ?? date;
  if (!iso) return '';
  try { return new Date(iso).toLocaleDateString(undefined, { year:'numeric', month:'short', day:'numeric' }); }
  catch (_) { return String(iso); }
}

function statusCls(s) {
  if (!s) return '';
  const l = s.toLowerCase();
  if (l === 'open')                       return 'open';
  if (l === 'closed' || l === 'resolved') return 'closed';
  if (l.includes('progress'))            return 'inprogress';
  return '';
}

function esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}