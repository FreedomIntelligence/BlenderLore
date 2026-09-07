/* =========================================================
   surflo.js — 3D-Coding-Blender project page interactions

   Three independent pieces:
     1) Interactive hero scenes — GLB viewers and a rendered fur turntable
     2) Pipeline animation — token sparkle, N-cycle, output point cloud,
        and a continuous particle flow between blocks.
     3) Lazy mesh / point-cloud viewers for the gallery and comparison
        strips. Each <canvas> is initialised the first time it scrolls
        into view and torn down (in principle) if it leaves.
   ========================================================= */
import * as THREE from 'three';
import { PLYLoader } from 'three/addons/loaders/PLYLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const ACCENT = 0x4d8dff;
const PLY_CACHE = (window.__surfloPlyCache = window.__surfloPlyCache || {});

/* --------------------------------------------------------------
   Per-asset transforms.

   Every viewer applies DEFAULT_VIEWER_TRANSFORM first; values in
   VIEWER_TRANSFORMS[url] (if any) override that asset's defaults.
   You can edit the object below by hand to fine-tune individual
   meshes / point clouds without touching anything else.

   The transform is BAKED into the geometry after auto-centering,
   so the rotation pivot stays at the world origin.
   -------------------------------------------------------------- */
const DEFAULT_VIEWER_TRANSFORM = {
  // PLYs in slides_nicolas/ are exported with Y-down. Flip 180° around X
  // so they render right-side up.
  rotation: { x: Math.PI, y: 0, z: 0 },
  scale: 1.0,
  offset: { x: 0, y: 0, z: 0 },
};

const VIEWER_TRANSFORMS = {
  // Examples — uncomment and tune per asset:
  // '../slides_nicolas/assets/mesh/garden_mv_16.ply': {
  //   rotation: { x: Math.PI, y: 0, z: 0 },
  //   scale: 1.1,
  //   offset: { x: 0, y: 0.05, z: 0 },
  // },
  // '../slides_nicolas/assets/pc/woody_mv_16.ply': {
  //   rotation: { x: Math.PI, y: Math.PI, z: 0 },
  // },
};

function resolveTransform(url) {
  const o = VIEWER_TRANSFORMS[url] || {};
  return {
    rotation: { ...DEFAULT_VIEWER_TRANSFORM.rotation, ...(o.rotation || {}) },
    scale:    o.scale  ?? DEFAULT_VIEWER_TRANSFORM.scale,
    offset:   { ...DEFAULT_VIEWER_TRANSFORM.offset, ...(o.offset || {}) },
  };
}


/* --------------------------------------------------------------
   1. Hero mesh
   -------------------------------------------------------------- */
const HERO_ANIMATIONS = {
  'holographic-card': {
    url: './assets/models/holographic-card.glb?v=20260830-holo-hero1',
    offsetY: 0.22,
    kind: 'card',
  },
  'chromatic-duck': {
    url: './assets/models/chromatic-duck.glb?v=20260822fixed2',
    offsetY: 0.16,
    kind: 'duck',
  },
  'stained-glass-window': {
    url: './assets/models/stained-glass-window.glb?v=20260829reorg1',
    offsetY: 0,
    kind: 'stained-glass',
    targetSize: 1.8,
    mobileTargetSize: 0.82,
    initialRotationY: -0.4,
    autoRotate: 0.000025,
  },
  maoqiu: {
    url: './assets/models/maoqiu.glb?v=20260830-original1',
    offsetY: 0.06,
    kind: 'maoqiu',
    targetSize: 1.52,
    mobileTargetSize: 0.94,
    // This asset is already a dense baked fur mesh. Rendering it through the
    // generic particle reveal downsamples ~828k hair vertices and leaves the
    // hero looking incomplete while adding unnecessary GPU work.
    reveal: false,
    autoRotate: 0.00007,
  },
};

function startHero() {
  const canvas = document.getElementById('hero-canvas');
  if (!canvas) return;

  const furPoster = document.getElementById('hero-fur-poster');
  const furVideo = document.getElementById('hero-fur-video');
  const heroStage = document.getElementById('hero-animation-panel');
  const controls = document.querySelector('.hero-animation-controls');
  const tabs = Array.from(document.querySelectorAll('.hero-animation-tab'));
  const copies = Array.from(document.querySelectorAll('.hero-asset-copy'));
  let activeKey = '';
  let heroInstance = null;
  let loadGeneration = 0;
  let furDragging = false;
  let furDragX = 0;
  let furWasPlaying = false;

  furVideo?.addEventListener('pointerdown', (event) => {
    if (!furVideo.classList.contains('is-visible')) return;
    furDragging = true;
    furDragX = event.clientX;
    furWasPlaying = !furVideo.paused;
    furVideo.pause();
    furVideo.classList.add('is-dragging');
    furVideo.setPointerCapture?.(event.pointerId);
    event.preventDefault();
  });
  furVideo?.addEventListener('pointermove', (event) => {
    if (!furDragging || !Number.isFinite(furVideo.duration) || furVideo.duration <= 0) return;
    const delta = event.clientX - furDragX;
    furDragX = event.clientX;
    const nextTime = furVideo.currentTime - delta * (furVideo.duration / Math.max(window.innerWidth, 1)) * 1.8;
    furVideo.currentTime = ((nextTime % furVideo.duration) + furVideo.duration) % furVideo.duration;
    event.preventDefault();
  });
  const finishFurDrag = (event) => {
    if (!furDragging) return;
    furDragging = false;
    furVideo?.classList.remove('is-dragging');
    furVideo?.releasePointerCapture?.(event.pointerId);
    if (furWasPlaying && furVideo?.classList.contains('is-visible')) furVideo.play().catch(() => {});
  };
  furVideo?.addEventListener('pointerup', finishFurDrag);
  furVideo?.addEventListener('pointercancel', finishFurDrag);

  function tryStart() {
    if (typeof window.initHeroGLB !== 'function') {
      setTimeout(tryStart, 60);
      return;
    }

    function selectAnimation(key) {
      const item = HERO_ANIMATIONS[key];
      if (!item || (key === activeKey && heroInstance)) return;

      activeKey = key;
      const generation = ++loadGeneration;
      const usesFurPreview = key === 'maoqiu';
      if (heroInstance) heroInstance.destroy();
      heroInstance = null;

      canvas.classList.toggle('is-hidden', usesFurPreview);
      heroStage?.classList.toggle('is-fur-preview', usesFurPreview);
      furPoster?.classList.toggle('is-visible', usesFurPreview);
      furVideo?.classList.toggle('is-visible', usesFurPreview);
      if (usesFurPreview) {
        if (furVideo) {
          furVideo.currentTime = 0;
          furVideo.play().catch(() => {});
        }
      } else {
        furVideo?.pause();
      }

      tabs.forEach((tab) => {
        const selected = tab.dataset.heroAnimation === key;
        tab.classList.toggle('is-active', selected);
        tab.setAttribute('aria-selected', String(selected));
        tab.tabIndex = selected ? 0 : -1;
      });

      copies.forEach((copy) => {
        const selected = copy.dataset.heroCopy === key;
        copy.hidden = !selected;
        copy.setAttribute('aria-hidden', String(!selected));
      });

      controls?.classList.add('is-loading');
      canvas.classList.add('is-switching');

      const initializer = item.renderer === 'turntable'
        ? window.initHeroTurntable
        : window.initHeroGLB;
      if (typeof initializer !== 'function') {
        controls?.classList.remove('is-loading');
        canvas.classList.remove('is-switching');
        return;
      }
      heroInstance = initializer(canvas, {
        url: item.url,
        atlasUrl: item.atlasUrl,
        columns: item.columns,
        rows: item.rows,
        frameCount: item.frameCount,
        autoRotate: item.autoRotate ?? 0.00013,
        offsetY: item.offsetY,
        scale: item.scale,
        pixelsPerFrame: item.pixelsPerFrame,
        kind: item.kind,
        reveal: item.reveal,
        targetSize: item.targetSize,
        mobileTargetSize: item.mobileTargetSize,
        initialRotationY: item.initialRotationY,
        onReady() {
          if (generation !== loadGeneration) return;
          controls?.classList.remove('is-loading');
          canvas.classList.remove('is-switching');
        },
        onError() {
          if (generation !== loadGeneration) return;
          controls?.classList.remove('is-loading');
          canvas.classList.remove('is-switching');
        },
      });

      if (!heroInstance) {
        controls?.classList.remove('is-loading');
        canvas.classList.remove('is-switching');
      }
    }

    tabs.forEach((tab, index) => {
      tab.addEventListener('click', () => selectAnimation(tab.dataset.heroAnimation));
      tab.addEventListener('keydown', (event) => {
        let nextIndex = null;
        if (event.key === 'ArrowRight' || event.key === 'ArrowDown') nextIndex = (index + 1) % tabs.length;
        if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') nextIndex = (index - 1 + tabs.length) % tabs.length;
        if (event.key === 'Home') nextIndex = 0;
        if (event.key === 'End') nextIndex = tabs.length - 1;
        if (nextIndex === null) return;
        event.preventDefault();
        tabs[nextIndex].focus();
        selectAnimation(tabs[nextIndex].dataset.heroAnimation);
      });
    });

    const initialTab = tabs.find((tab) => tab.getAttribute('aria-selected') === 'true') || tabs[0];
    selectAnimation(initialTab?.dataset.heroAnimation || 'holographic-card');
  }
  tryStart();
}

/* Reuse the stained-glass GLB renderer in the workflow's final output. */
function setupWorkflowHeroPreview() {
  const canvas = document.querySelector('[data-workflow-hero="stained-glass-window"]');
  if (!canvas) return;

  const tryStart = () => {
    if (typeof window.initHeroGLB !== 'function') {
      window.setTimeout(tryStart, 60);
      return;
    }
    const item = HERO_ANIMATIONS['stained-glass-window'];
    window.initHeroGLB(canvas, {
      url: item.url,
      autoRotate: 0.00004,
      forceAutoRotate: true,
      offsetY: item.offsetY,
      kind: item.kind,
      targetSize: item.targetSize * 0.7,
      mobileTargetSize: item.mobileTargetSize * 0.7,
      initialRotationY: item.initialRotationY,
      interactive: false,
      reveal: false,
      preserveDrawingBuffer: true,
    });
  };

  tryStart();
}

/* --------------------------------------------------------------
   2. Pipeline animation
   -------------------------------------------------------------- */
function setupPipeline() {
  const root = document.getElementById('surflo-pipeline');
  if (!root) return;

  // ---- Latent tokens (8 x 16 = 128) ----
  const tokenHost = root.querySelector('.pipe-tokens');
  if (tokenHost && !tokenHost.childElementCount) {
    for (let i = 0; i < 128; i++) {
      const t = document.createElement('span');
      t.className = 'pipe-token';
      t.style.opacity = String(0.35 + Math.random() * 0.55);
      tokenHost.appendChild(t);
    }
    // Sparkle: every ~60ms, randomly jiggle one token's opacity
    setInterval(() => {
      const all = tokenHost.children;
      if (!all.length) return;
      const k = Math.floor(Math.random() * all.length);
      all[k].style.opacity = String(0.3 + Math.random() * 0.7);
    }, 80);
  }

  // ---- N counter cycle ----
  const N_VALUES = [3, 9, 16, 33, 65, 9];
  const counterEl = document.getElementById('pipe-counter-n');
  const stack = root.querySelector('.pipe-imagestack');
  const imgs = stack ? stack.querySelectorAll('.pipe-img') : [];
  let cycleIdx = 0;
  function tickCycle() {
    const n = N_VALUES[cycleIdx % N_VALUES.length];
    if (counterEl) counterEl.textContent = String(n);
    const visible = Math.min(n, imgs.length);
    imgs.forEach((img, idx) => {
      img.classList.toggle('hidden', idx >= visible);
    });
    cycleIdx++;
  }
  tickCycle();
  setInterval(tickCycle, 2800);

  // ---- Output canvas: rotating procedural point cloud ----
  const outCanvas = document.getElementById('pipe-output-canvas');
  if (outCanvas) {
    const ctx = outCanvas.getContext('2d');
    // Sample points on a "cute object" (a rounded torso-like blob)
    const N = 850;
    const pts = new Array(N);
    for (let i = 0; i < N; i++) {
      const u = Math.random();
      const v = Math.random();
      const theta = u * Math.PI * 2;
      const phi = Math.acos(2 * v - 1);
      // soft potato shape
      const r = 0.62
        + 0.10 * Math.sin(3 * theta + phi)
        + 0.06 * Math.cos(2 * phi);
      pts[i] = {
        x: r * Math.sin(phi) * Math.cos(theta),
        y: r * Math.sin(phi) * Math.sin(theta) - 0.05,
        z: r * Math.cos(phi),
      };
    }
    let angle = 0;
    let running = true;
    let pausedByVisibility = false;
    const io = new IntersectionObserver(
      ([entry]) => { pausedByVisibility = !entry.isIntersecting; },
      { threshold: 0.01 }
    );
    io.observe(outCanvas);

    function frame() {
      if (!running) return;
      requestAnimationFrame(frame);
      if (pausedByVisibility || document.hidden) return;

      // Match canvas internal size to its CSS size for crispness
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const w = outCanvas.clientWidth || 220;
      const h = outCanvas.clientHeight || 130;
      if (outCanvas.width !== w * dpr || outCanvas.height !== h * dpr) {
        outCanvas.width = w * dpr;
        outCanvas.height = h * dpr;
      }
      const W = outCanvas.width, H = outCanvas.height;

      ctx.clearRect(0, 0, W, H);
      angle += 0.008;

      const cx = W / 2;
      const cy = H / 2;
      const sa = Math.sin(angle);
      const ca = Math.cos(angle);
      const focal = Math.min(W, H) * 0.55;

      // Pre-sort by z for back-to-front rendering
      const proj = pts.map((p) => {
        const x = p.x * ca - p.z * sa;
        const z = p.x * sa + p.z * ca;
        const y = p.y;
        const denom = (2.4 - z);
        const sz = focal / denom;
        return {
          px: cx + x * sz,
          py: cy + y * sz,
          z, sz,
        };
      }).sort((a, b) => a.z - b.z);

      for (const q of proj) {
        const a = Math.max(0, Math.min(1, (q.z + 1.0) * 0.45 + 0.1));
        const r = Math.max(0.6, q.sz * 0.018);
        ctx.fillStyle = `rgba(77, 141, 255, ${a})`;
        ctx.beginPath();
        ctx.arc(q.px, q.py, r, 0, Math.PI * 2);
        ctx.fill();
      }
    }
    requestAnimationFrame(frame);
  }

  // ---- Particle flow between blocks ----
  const flow = document.getElementById('pipe-flow-canvas');
  if (flow && root) {
    const fctx = flow.getContext('2d');

    // Compute "lanes" (horizontal lines) between each adjacent pair of blocks
    const blocks = [
      root.querySelector('.pipe-block-images'),
      root.querySelector('.pipe-block-encoder'),
      root.querySelector('.pipe-block-latent'),
      root.querySelector('.pipe-block-decoder'),
      root.querySelector('.pipe-block-output'),
    ];

    const particles = [];
    const MAX_PARTICLES = 80;

    function syncSize() {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const w = root.clientWidth;
      const h = root.clientHeight;
      if (flow.width !== w * dpr || flow.height !== h * dpr) {
        flow.width = w * dpr;
        flow.height = h * dpr;
      }
      flow.style.width = w + 'px';
      flow.style.height = h + 'px';
      fctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    syncSize();
    window.addEventListener('resize', syncSize);
    if ('ResizeObserver' in window) {
      new ResizeObserver(syncSize).observe(root);
    }

    function spawnParticle() {
      // Choose a random adjacent block pair
      const i = Math.floor(Math.random() * (blocks.length - 1));
      const a = blocks[i];
      const b = blocks[i + 1];
      if (!a || !b) return;
      const rootRect = root.getBoundingClientRect();
      const ra = a.getBoundingClientRect();
      const rb = b.getBoundingClientRect();
      const x0 = ra.right - rootRect.left;
      const x1 = rb.left  - rootRect.left;
      const yc = (ra.top + ra.bottom) / 2 - rootRect.top;
      // Distinct color per lane: encoder=accent, latent=lav, decoder=accent, output=accent
      const colors = ['#4d8dff', '#a78bfa', '#a78bfa', '#ff8a3d'];
      particles.push({
        x: x0,
        y: yc + (Math.random() - 0.5) * 22,
        x0, x1,
        y0: yc + (Math.random() - 0.5) * 22,
        born: performance.now(),
        life: 1100 + Math.random() * 700,
        color: colors[i] || '#4d8dff',
      });
    }
    let lastSpawn = performance.now();

    let running = true;
    let visible = true;
    const ioFlow = new IntersectionObserver(
      ([entry]) => { visible = entry.isIntersecting; },
      { threshold: 0.01 }
    );
    ioFlow.observe(root);

    function frame(now) {
      if (!running) return;
      requestAnimationFrame(frame);
      if (!visible || document.hidden) return;

      const W = flow.clientWidth, H = flow.clientHeight;

      // Spawn
      if (particles.length < MAX_PARTICLES && now - lastSpawn > 80) {
        spawnParticle();
        lastSpawn = now;
      }

      fctx.clearRect(0, 0, W, H);

      for (let i = particles.length - 1; i >= 0; i--) {
        const p = particles[i];
        const t = (now - p.born) / p.life;
        if (t > 1) { particles.splice(i, 1); continue; }
        // ease-in-out
        const e = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
        const x = p.x0 + (p.x1 - p.x0) * e;
        const y = p.y0;
        const a = Math.sin(t * Math.PI) * 0.85;

        // Soft glow: two passes
        fctx.fillStyle = p.color;
        fctx.globalAlpha = a * 0.18;
        fctx.beginPath(); fctx.arc(x, y, 6, 0, Math.PI * 2); fctx.fill();
        fctx.globalAlpha = a;
        fctx.beginPath(); fctx.arc(x, y, 1.6, 0, Math.PI * 2); fctx.fill();
      }
      fctx.globalAlpha = 1;
    }
    requestAnimationFrame(frame);
  }
}

/* --------------------------------------------------------------
   3. Lazy mesh / point-cloud viewers
   -------------------------------------------------------------- */
function makeParticleSprite() {
  const size = 64;
  const c = document.createElement('canvas');
  c.width = c.height = size;
  const ctx = c.getContext('2d');
  const r = size / 2;
  const g = ctx.createRadialGradient(r, r, 0, r, r, r);
  g.addColorStop(0.0, 'rgba(200, 255, 245, 1)');
  g.addColorStop(0.3, 'rgba(77, 141, 255, 0.85)');
  g.addColorStop(0.7, 'rgba(77, 141, 255, 0.18)');
  g.addColorStop(1.0, 'rgba(77, 141, 255, 0)');
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, size, size);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

/**
 * Create a small Three.js scene that renders a PLY (mesh or points) with
 * the same "particle assembly" reveal as the hero mesh, plus mouse
 * rotation / wheel zoom via OrbitControls.
 *
 * @param {HTMLCanvasElement} canvas
 * @param {Object} opts { url, kind: 'mesh' | 'points',
 *                         autoRotateSpeed, enableZoom, transform }
 */
function createSurfloViewer(canvas, opts = {}) {
  if (!canvas) return null;
  const url = opts.url;
  const kind = opts.kind || 'mesh';
  if (!url) { console.warn('[surflo] viewer needs a url'); return null; }

  // Resolve transform: defaults < global override < per-call override
  const tx = (() => {
    const base = resolveTransform(url);
    const o = opts.transform || {};
    return {
      rotation: { ...base.rotation, ...(o.rotation || {}) },
      scale:    o.scale  ?? base.scale,
      offset:   { ...base.offset, ...(o.offset || {}) },
    };
  })();

  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({
      canvas, antialias: true, alpha: true, powerPreference: 'high-performance',
    });
  } catch (e) {
    console.warn('[surflo] WebGL unavailable for', url, e);
    return null;
  }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setClearColor(0x000000, 0);
  renderer.outputColorSpace = THREE.SRGBColorSpace;

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(34, 1, 0.01, 100);
  // Starting dolly distance (camera looks at the scene centre). Lower = closer.
  const camZ = Number.isFinite(opts.cameraZ) ? opts.cameraZ : 3.0;
  // Optional scene-centre shift: moves both the orbit target and the camera
  // so a different point ends up framed at the centre of the viewport.
  const ctr = opts.center || {};
  const cx = Number.isFinite(ctr.x) ? ctr.x : 0;
  const cy = Number.isFinite(ctr.y) ? ctr.y : 0;
  const cz = Number.isFinite(ctr.z) ? ctr.z : 0;
  camera.position.set(cx, cy, cz + camZ);

  // Lights (kept world-space; the model is a static, OrbitControls moves
  // the camera around it).
  scene.add(new THREE.AmbientLight(0xffffff, 0.55));
  const key = new THREE.DirectionalLight(0xffffff, 1.0);
  key.position.set(2, 3, 4);
  scene.add(key);
  const rim = new THREE.DirectionalLight(ACCENT, 1.4);
  rim.position.set(-3, 0.5, -2);
  scene.add(rim);

  // Interactivity — orbit + pan + unconstrained zoom on every viewer.
  // Mouse:  left = orbit, right = pan, wheel = zoom
  // Touch:  one finger = orbit, two fingers = pan + pinch-zoom
  const reduceMotion = window.matchMedia &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.enableRotate = true;
  controls.enablePan = true;
  controls.screenSpacePanning = true;   // pan in screen plane (more intuitive)
  controls.panSpeed = 0.9;
  controls.enableZoom = true;
  controls.zoomSpeed = 0.9;
  controls.zoomToCursor = true;
  // Unconstrained zoom: leave .minDistance / .maxDistance at their defaults
  // (0 / Infinity) so the user can dolly arbitrarily close or far.
  controls.rotateSpeed = 0.75;
  controls.autoRotate = !reduceMotion;
  controls.autoRotateSpeed = opts.autoRotateSpeed ?? 0.7;
  // Orbit around the (optionally shifted) scene centre.
  controls.target.set(cx, cy, cz);
  controls.update();
  // Once the user grabs the model, stop auto-spinning.
  controls.addEventListener('start', () => { controls.autoRotate = false; });

  let mesh = null;
  let baseMat = null;        // vertex-color (unlit) or green Phong
  let normalMat = null;
  let useNormalShading = !!opts.normalShading;
  let points = null;
  let revealAnim = null;
  const sprite = makeParticleSprite();

  function buildFromGeometry(geometry) {
    // Auto center + uniform scale so every asset fits a unit-ish box
    geometry.computeBoundingBox();
    const bbox = geometry.boundingBox;
    const center = bbox.getCenter(new THREE.Vector3());
    const size = bbox.getSize(new THREE.Vector3());
    const maxDim = Math.max(size.x, size.y, size.z) || 1;
    geometry.translate(-center.x, -center.y, -center.z);
    const targetSize = 1.55 * tx.scale;
    const s = targetSize / maxDim;
    geometry.scale(s, s, s);

    // Bake user transform (rotation + offset) so the rotation pivot
    // stays at world origin and OrbitControls orbits around the
    // *visual* centre.
    const r = tx.rotation;
    if (r.x) geometry.rotateX(r.x);
    if (r.y) geometry.rotateY(r.y);
    if (r.z) geometry.rotateZ(r.z);
    const o = tx.offset;
    if (o.x || o.y || o.z) geometry.translate(o.x, o.y, o.z);

    if (kind === 'mesh' && geometry.index) {
      geometry.computeVertexNormals();
      // Default material -----------------------------------------------
      // If the PLY ships per-vertex RGB, render it unlit (emissive style)
      // with the raw colors. Otherwise fall back to the original green
      // Phong look so untextured meshes still look like Surflo output.
      const hasVertexColors = !!geometry.attributes.color;
      if (hasVertexColors) {
        baseMat = new THREE.MeshBasicMaterial({
          vertexColors: true,
          // No fog, no lighting — MeshBasicMaterial is already unlit, so
          // each vertex colour passes straight through (modulo gamma).
          transparent: true,
          opacity: 0,
          side: THREE.DoubleSide,
          toneMapped: false,
        });
      } else {
        baseMat = new THREE.MeshPhongMaterial({
          color: ACCENT,
          specular: 0xffffff,
          shininess: 35,
          emissive: ACCENT,
          emissiveIntensity: 0.18,
          flatShading: true,
          transparent: true,
          opacity: 0,
          side: THREE.DoubleSide,
        });
      }
      // "Normal shading" material ------------------------------------
      // MeshNormalMaterial bakes the view-space normal into RGB with
      // no lights involved (effectively an emission shader).
      // Colors update naturally as the camera moves.
      //
      // We patch the fragment-shader output via onBeforeCompile so the
      // colors are softened: a small desaturation, pull toward midgrey,
      // and a tiny gamma — keeps the "normals as colors" intuition but
      // reads as pastel rather than harsh primaries.
      normalMat = new THREE.MeshNormalMaterial({
        flatShading: true,
        transparent: true,
        opacity: 0,
        side: THREE.DoubleSide,
      });
      normalMat.onBeforeCompile = (shader) => {
        shader.fragmentShader = shader.fragmentShader.replace(
          'gl_FragColor = vec4( packNormalToRGB( normal ), opacity );',
          /* glsl */ `{
            vec3 _raw   = packNormalToRGB( normal );
            float _gray = dot( _raw, vec3( 0.3333 ) );
            // 28% desaturation
            vec3 _desat = mix( _raw, vec3( _gray ), 0.28 );
            // pull every channel a touch toward midgrey for that pastel feel
            vec3 _soft  = mix( vec3( 0.55 ), _desat, 0.82 );
            // round off the extremes
            _soft = pow( _soft, vec3( 0.92 ) );
            gl_FragColor = vec4( _soft, opacity );
          }`
        );
      };
      // Stable cache key so Three.js can reuse the compiled program
      // across instances of this softened material.
      normalMat.customProgramCacheKey = () => 'surflo-soft-normals-v1';
      mesh = new THREE.Mesh(geometry, useNormalShading ? normalMat : baseMat);
      scene.add(mesh);
    }

    setupParticleReveal(geometry);
  }

  function setupParticleReveal(geometry) {
    const fullPos = geometry.attributes.position.array;
    const N_total = geometry.attributes.position.count;
    const MAX = 30000;  // small viewer = lower particle budget
    const stride = Math.max(1, Math.floor(N_total / MAX));
    const N = Math.floor(N_total / stride);

    // Plain RGB mode: opaque, unshaded points using the PLY's per-vertex
    // colours (emission look). Only engaged when the caller opts in AND
    // the geometry actually carries a colour attribute.
    const colorAttr = geometry.attributes.color;
    const plainColors = !!opts.plainColors && !!colorAttr;
    const srcCol = plainColors ? colorAttr.array : null;

    const finalArr = new Float32Array(N * 3);
    const startArr = new Float32Array(N * 3);
    const curArr = new Float32Array(N * 3);
    const colArr = plainColors ? new Float32Array(N * 3) : null;

    for (let i = 0; i < N; i++) {
      const s = i * stride * 3;
      finalArr[i * 3]     = fullPos[s];
      finalArr[i * 3 + 1] = fullPos[s + 1];
      finalArr[i * 3 + 2] = fullPos[s + 2];

      if (plainColors) {
        colArr[i * 3]     = srcCol[s];
        colArr[i * 3 + 1] = srcCol[s + 1];
        colArr[i * 3 + 2] = srcCol[s + 2];
      }

      const u = Math.random(), v = Math.random();
      const theta = 2 * Math.PI * u;
      const phi = Math.acos(2 * v - 1);
      const r = 1.6 + Math.random() * 1.4;
      startArr[i * 3]     = r * Math.sin(phi) * Math.cos(theta);
      startArr[i * 3 + 1] = r * Math.sin(phi) * Math.sin(theta);
      startArr[i * 3 + 2] = r * Math.cos(phi);
    }
    curArr.set(startArr);

    const pgeo = new THREE.BufferGeometry();
    pgeo.setAttribute('position', new THREE.BufferAttribute(curArr, 3));
    if (plainColors) {
      pgeo.setAttribute('color', new THREE.BufferAttribute(colArr, 3));
    }
    const pmat = plainColors
      ? new THREE.PointsMaterial({
          // Raw vertex colours, no sprite/tint, no additive glow:
          // each point is a flat, opaque dot in its own colour.
          size: 3.0,
          vertexColors: true,
          transparent: true,   // only so the reveal can fade opacity 0->1
          opacity: 0,
          depthWrite: true,
          depthTest: true,
          blending: THREE.NormalBlending,
          sizeAttenuation: false,
          toneMapped: false,
        })
      : new THREE.PointsMaterial({
          size: kind === 'points' ? 0.018 : 0.020,
          map: sprite,
          color: 0xc8fff0,
          transparent: true,
          opacity: 0,
          blending: THREE.AdditiveBlending,
          depthWrite: false,
          sizeAttenuation: true,
        });
    points = new THREE.Points(pgeo, pmat);
    scene.add(points);

    revealAnim = {
      elapsed: 0,
      fadeInDur: 280,
      gatherDur: 1500,
      crossfadeDur: kind === 'mesh' ? 600 : 0,
      keepPoints: kind === 'points',
      startArr, curArr, finalArr,
      pgeo, pmat,
    };
  }

  function tickReveal(dt) {
    if (!revealAnim) return;
    const a = revealAnim;
    a.elapsed += dt;
    const t = a.elapsed;
    const gT = Math.min(1, t / a.gatherDur);
    const eG = 1 - Math.pow(1 - gT, 4);
    const sa = a.startArr, fa = a.finalArr, ca = a.curArr;
    for (let i = 0; i < ca.length; i++) {
      ca[i] = sa[i] + (fa[i] - sa[i]) * eG;
    }
    a.pgeo.attributes.position.needsUpdate = true;

    const fIn = Math.min(1, t / a.fadeInDur);
    let pAlpha = fIn;
    let mAlpha = 0;
    if (t > a.gatherDur && a.crossfadeDur > 0) {
      const cf = Math.min(1, (t - a.gatherDur) / a.crossfadeDur);
      pAlpha = 1 - cf;
      mAlpha = cf;
    }
    a.pmat.opacity = a.keepPoints ? Math.min(1, pAlpha) : pAlpha;
    if (mesh) mesh.material.opacity = mAlpha;

    const totalDur = a.gatherDur + a.crossfadeDur;
    if (t >= totalDur) {
      if (!a.keepPoints && points) {
        scene.remove(points);
        a.pgeo.dispose();
        a.pmat.dispose();
        points = null;
      }
      if (mesh && !a.keepPoints) {
        mesh.material.transparent = false;
        mesh.material.opacity = 1;
        mesh.material.needsUpdate = true;
      }
      revealAnim = null;
    }
  }

  function resize() {
    const w = canvas.clientWidth || 300;
    const h = canvas.clientHeight || 300;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }
  resize();
  let ro;
  if ('ResizeObserver' in window) {
    ro = new ResizeObserver(resize);
    ro.observe(canvas);
  } else {
    window.addEventListener('resize', resize);
  }

  // Pause when off-screen
  let visible = true;
  const ioVis = new IntersectionObserver(
    ([entry]) => { visible = entry.isIntersecting; },
    { threshold: 0.05 }
  );
  ioVis.observe(canvas);

  let running = true;
  let last = performance.now();
  function frame(now) {
    if (!running) return;
    requestAnimationFrame(frame);
    if (!visible || document.hidden) { last = now; return; }
    const dt = Math.min(50, now - last);
    last = now;

    controls.update();
    tickReveal(dt);
    renderer.render(scene, camera);
  }
  requestAnimationFrame(frame);

  // Load PLY (with cache)
  if (PLY_CACHE[url]) {
    buildFromGeometry(PLY_CACHE[url].clone());
  } else {
    const loader = new PLYLoader();
    loader.load(
      url,
      (geometry) => {
        PLY_CACHE[url] = geometry;
        buildFromGeometry(geometry.clone());
      },
      undefined,
      (err) => console.error('[surflo] failed to load', url, err)
    );
  }

  function setNormalShading(on) {
    useNormalShading = !!on;
    if (!mesh || !baseMat || !normalMat) return;
    const target = useNormalShading ? normalMat : baseMat;
    // Preserve in-flight reveal opacity & transparency state.
    target.opacity = mesh.material.opacity;
    target.transparent = mesh.material.transparent;
    target.needsUpdate = true;
    mesh.material = target;
  }

  return {
    setNormalShading,
    destroy() {
      running = false;
      try { ro && ro.disconnect(); } catch (e) {}
      try { ioVis.disconnect(); } catch (e) {}
      try { controls.dispose(); } catch (e) {}
      if (mesh) {
        try { mesh.geometry.dispose(); } catch (e) {}
      }
      try { baseMat && baseMat.dispose(); } catch (e) {}
      try { normalMat && normalMat.dispose(); } catch (e) {}
      if (points) {
        try { points.geometry.dispose(); } catch (e) {}
        try { points.material.dispose(); } catch (e) {}
      }
      try { sprite.dispose(); } catch (e) {}
      try { renderer.dispose(); } catch (e) {}
    },
  };
}

function setupViewers() {
  // Strip-card grids (multi-resolution, guidance) — small comparison
  // strips, kept as side-by-side viewers, lazy-initialised.
  const stripCanvases = document.querySelectorAll('.strip-card-canvas');
  if (stripCanvases.length) {
    const io = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting && !entry.target.dataset.surfloInit) {
          entry.target.dataset.surfloInit = '1';
          const el = entry.target;
          createSurfloViewer(el, {
            url: el.dataset.ply,
            kind: el.dataset.kind || 'mesh',
          });
        }
      }
    }, { rootMargin: '300px 0px', threshold: 0.01 });
    stripCanvases.forEach((c) => io.observe(c));
  }

  // Every <div class="viewer-carousel"> becomes its own one-viewer-at-a-time
  // carousel that swaps the active mesh/point-cloud on demand.
  document.querySelectorAll('.viewer-carousel').forEach(setupCarousel);
}

/* --------------------------------------------------------------
   Generic single-viewer carousel.

   For any element <div class="viewer-carousel" data-items='[...]'
                          data-kind="mesh|points"> ... </div>
   it wires:
     - the .viewer-carousel-tab buttons to switch active item,
     - .viewer-carousel-prev / -next arrow buttons,
     - keyboard ArrowLeft / ArrowRight,
     - and ensures only ONE Three.js viewer is alive at a time.
   -------------------------------------------------------------- */
function setupCarousel(root) {
  if (!root) return;

  let items = [];
  try {
    items = JSON.parse(root.dataset.items || '[]');
  } catch (e) {
    console.error('[surflo] could not parse carousel items', e);
    return;
  }
  if (!items.length) return;

  const kind   = root.dataset.kind || 'mesh';
  // Opt-in per-carousel point style. "rgb" => render points with their
  // raw per-vertex colours, opaque and unshaded (emission look), instead
  // of the default glowy green additive particles.
  const plainColors = (root.dataset.pointStyle || '') === 'rgb';
  // Optional per-carousel starting camera distance (closer = smaller).
  const camZAttr = parseFloat(root.dataset.camZ);
  const camZ = Number.isFinite(camZAttr) ? camZAttr : undefined;
  const stage  = root.querySelector('.viewer-carousel-stage');
  if (!stage) return;
  const tabs   = root.querySelectorAll('.viewer-carousel-tab');
  const nameEl = root.querySelector('[data-role="name"]');
  const metaEl = root.querySelector('[data-role="meta"]');
  const prevBtn  = root.querySelector('.viewer-carousel-prev');
  const nextBtn  = root.querySelector('.viewer-carousel-next');
  const loader   = root.querySelector('.viewer-carousel-loader');
  const shadeBtn = root.querySelector('.viewer-carousel-shade');

  let activeIdx     = 0;
  let currentViewer = null;
  let initialized   = false;
  // Persisted across mesh swaps: if the user enabled "Normals", the next
  // mesh they pick should also render in normal shading.
  let useNormalShading = false;

  function showLoader(on) {
    if (loader) loader.classList.toggle('is-loading', !!on);
  }

  function disposeCurrent() {
    if (currentViewer) {
      try { currentViewer.destroy(); } catch (e) {}
      currentViewer = null;
    }
    // Replace the canvas element so the next viewer gets a *fresh* WebGL
    // context — disposing a renderer alone doesn't reliably free the
    // underlying context across browsers.
    const old = stage.querySelector('.viewer-carousel-canvas');
    if (old && old.parentNode) {
      const fresh = document.createElement('canvas');
      fresh.className = 'viewer-carousel-canvas';
      fresh.setAttribute('aria-hidden', 'true');
      old.parentNode.replaceChild(fresh, old);
    }
  }

  function show(idx) {
    const n = items.length;
    idx = ((idx % n) + n) % n;
    activeIdx = idx;
    const item = items[idx];

    if (nameEl) nameEl.textContent = item.name || '';
    if (metaEl) metaEl.textContent = item.meta || '';
    tabs.forEach((t, i) => {
      const on = i === idx;
      t.classList.toggle('is-active', on);
      t.setAttribute('aria-selected', on ? 'true' : 'false');
    });

    stage.classList.add('is-fading');
    showLoader(true);

    setTimeout(() => {
      disposeCurrent();
      const canvas = stage.querySelector('.viewer-carousel-canvas');
      currentViewer = createSurfloViewer(canvas, {
        url: item.url,
        kind: item.kind || kind,
        normalShading: useNormalShading,
        plainColors,
        // Per-item camera framing overrides the carousel-level default, so
        // each "N views" point cloud can be centred/zoomed independently.
        cameraZ: Number.isFinite(item.camZ) ? item.camZ : camZ,
        center: item.center,
      });
      requestAnimationFrame(() => {
        stage.classList.remove('is-fading');
        const url = item.url;
        const tStart = performance.now();
        function pollCache() {
          const cached = window.__surfloPlyCache && window.__surfloPlyCache[url];
          if (cached || performance.now() - tStart > 4000) {
            showLoader(false);
            return;
          }
          requestAnimationFrame(pollCache);
        }
        pollCache();
      });
    }, 230);
  }

  // Default item = whichever tab ships with `is-active` in the markup
  // (falls back to the first item).
  const defaultIdx = Math.max(
    0,
    Array.from(tabs).findIndex((t) => t.classList.contains('is-active'))
  );
  activeIdx = defaultIdx;

  // Init on first time the carousel scrolls into view.
  const io = new IntersectionObserver((entries) => {
    if (entries[0].isIntersecting && !initialized) {
      initialized = true;
      show(defaultIdx);
    }
  }, { rootMargin: '300px 0px', threshold: 0.01 });
  io.observe(root);

  tabs.forEach((tab, i) => {
    tab.addEventListener('click', () => { if (i !== activeIdx) show(i); });
  });
  if (prevBtn) prevBtn.addEventListener('click', () => show(activeIdx - 1));
  if (nextBtn) nextBtn.addEventListener('click', () => show(activeIdx + 1));
  if (shadeBtn) {
    shadeBtn.addEventListener('click', () => {
      useNormalShading = !useNormalShading;
      shadeBtn.classList.toggle('is-active', useNormalShading);
      shadeBtn.setAttribute('aria-pressed', useNormalShading ? 'true' : 'false');
      if (currentViewer && currentViewer.setNormalShading) {
        currentViewer.setNormalShading(useNormalShading);
      }
    });
  }

  // Keyboard arrows when the carousel area has focus.
  root.tabIndex = 0;
  root.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowLeft')  { e.preventDefault(); show(activeIdx - 1); }
    if (e.key === 'ArrowRight') { e.preventDefault(); show(activeIdx + 1); }
  });
}

/* --------------------------------------------------------------
   Boot
   -------------------------------------------------------------- */
function setupImageCompare() {
  document.querySelectorAll('.image-compare-stage').forEach((stage) => {
    const leftSide  = stage.querySelector('.image-compare-side-left');
    const rightSide = stage.querySelector('.image-compare-side-right');
    const divider   = stage.querySelector('.image-compare-divider');
    if (!divider || (!leftSide && !rightSide)) return;

    let pos = 0.5;

    function setPos(p) {
      pos = Math.max(0, Math.min(1, p));
      const pct = pos * 100;
      if (leftSide)  leftSide.style.clipPath  = `inset(0 ${100 - pct}% 0 0)`;
      if (rightSide) rightSide.style.clipPath = `inset(0 0 0 ${pct}%)`;
      divider.style.left = `${pct}%`;
      stage.setAttribute('aria-valuenow', String(Math.round(pct)));
    }

    function updateFromEvent(e) {
      const rect = stage.getBoundingClientRect();
      if (!rect.width) return;
      setPos((e.clientX - rect.left) / rect.width);
    }

    stage.addEventListener('pointerdown', (e) => {
      stage.classList.add('is-dragging');
      try { stage.setPointerCapture(e.pointerId); } catch (_) {}
      updateFromEvent(e);
    });
    stage.addEventListener('pointermove', (e) => {
      if (!stage.classList.contains('is-dragging')) return;
      updateFromEvent(e);
    });
    function endDrag(e) {
      stage.classList.remove('is-dragging');
      try { stage.releasePointerCapture(e.pointerId); } catch (_) {}
    }
    stage.addEventListener('pointerup', endDrag);
    stage.addEventListener('pointercancel', endDrag);
    stage.addEventListener('pointerleave', endDrag);

    // Click anywhere on the stage to jump there too.
    stage.addEventListener('click', updateFromEvent);

    // Keyboard navigation when focused.
    stage.addEventListener('keydown', (e) => {
      const step = e.shiftKey ? 0.10 : 0.04;
      if (e.key === 'ArrowLeft')  { setPos(pos - step); e.preventDefault(); }
      if (e.key === 'ArrowRight') { setPos(pos + step); e.preventDefault(); }
      if (e.key === 'Home')       { setPos(0); e.preventDefault(); }
      if (e.key === 'End')        { setPos(1); e.preventDefault(); }
    });

    setPos(0.5);
  });
}

/* Quantitative tabset: each .quant-tabset has a .quant-tablist of
   buttons and a .quant-panels container. Clicking (or arrow-keying)
   a tab toggles the matching panel into the active state. Switching
   uses CSS animation only — no layout thrash. */
function setupQuantTabs() {
  document.querySelectorAll('.quant-tabset').forEach((set) => {
    const tabs = Array.from(set.querySelectorAll('.quant-tab'));
    const panels = Array.from(set.querySelectorAll(':scope > .quant-panels > .quant-panel'));
    if (!tabs.length || !panels.length) return;

    function activate(idx) {
      idx = Math.max(0, Math.min(tabs.length - 1, idx));
      tabs.forEach((t, i) => {
        const on = i === idx;
        t.classList.toggle('is-active', on);
        t.setAttribute('aria-selected', on ? 'true' : 'false');
        t.tabIndex = on ? 0 : -1;
      });
      panels.forEach((p, i) => p.classList.toggle('is-active', i === idx));
    }

    tabs.forEach((tab, i) => {
      tab.addEventListener('click', () => activate(i));
      tab.addEventListener('keydown', (e) => {
        if (e.key === 'ArrowRight') {
          e.preventDefault();
          const next = (i + 1) % tabs.length;
          activate(next);
          tabs[next].focus();
        } else if (e.key === 'ArrowLeft') {
          e.preventDefault();
          const prev = (i - 1 + tabs.length) % tabs.length;
          activate(prev);
          tabs[prev].focus();
        } else if (e.key === 'Home') {
          e.preventDefault();
          activate(0);
          tabs[0].focus();
        } else if (e.key === 'End') {
          e.preventDefault();
          activate(tabs.length - 1);
          tabs[tabs.length - 1].focus();
        }
      });
    });
  });
}

/* Copy-to-clipboard for the citation block. Buttons carry a
   data-copy-target CSS selector pointing at the element whose text
   should be copied. Falls back to a temporary textarea + execCommand
   when the async Clipboard API is unavailable (e.g. file:// or http). */
function setupCiteCopy() {
  document.querySelectorAll('.cite-copy[data-copy-target]').forEach((btn) => {
    const target = document.querySelector(btn.getAttribute('data-copy-target'));
    if (!target) return;
    const label = btn.querySelector('span');
    const icon = btn.querySelector('i');
    const defaultText = label ? label.textContent : '';
    let resetTimer = null;

    function flash(ok) {
      if (label) label.textContent = ok ? 'Copied!' : 'Press Ctrl/Cmd+C';
      if (icon) icon.className = ok ? 'fa fa-check' : 'fa fa-clipboard';
      btn.classList.toggle('is-copied', ok);
      clearTimeout(resetTimer);
      resetTimer = setTimeout(() => {
        if (label) label.textContent = defaultText;
        if (icon) icon.className = 'fa fa-clipboard';
        btn.classList.remove('is-copied');
      }, 2000);
    }

    function legacyCopy(text) {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.position = 'absolute';
      ta.style.left = '-9999px';
      document.body.appendChild(ta);
      ta.select();
      let ok = false;
      try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
      document.body.removeChild(ta);
      return ok;
    }

    btn.addEventListener('click', () => {
      const text = target.innerText;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text)
          .then(() => flash(true))
          .catch(() => flash(legacyCopy(text)));
      } else {
        flash(legacyCopy(text));
      }
    });
  });
}

function boot() {
  startHero();
  setupWorkflowHeroPreview();
  setupPipeline();
  setupViewers();
  setupImageCompare();
  setupQuantTabs();
  setupCiteCopy();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}
