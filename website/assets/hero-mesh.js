/* =========================================================
   hero-mesh.js — Three.js hero scene that renders the
   decimated PLY mesh in the homepage hero stage.

   Behaviours:
   - Particle-assembly reveal: vertices first appear as a
     scattered cloud of soft green-glow sprites, then converge
     onto their final positions and cross-fade into the lit mesh.
   - Slow auto-rotation
   - Mouse parallax (subtle tilt + horizontal drift)
   - Pause when the canvas is off-screen (IntersectionObserver)
   - Honour prefers-reduced-motion (no auto-rotation)
   - Resize-aware (ResizeObserver)
   - Clean teardown via destroy() so lite-mode can swap it out

   Exposed:
     window.initHeroMesh(canvas, opts) -> { destroy }
   ========================================================= */
import * as THREE from 'three';
import { PLYLoader } from 'three/addons/loaders/PLYLoader.js';

const ACCENT = 0x4d8dff;

/* ---------------------------------------------------------------------------
   Hand-tunable mesh placement.
   Edit these values directly to adjust how the mesh sits with respect to the
   camera. They can also be overridden per call via opts.modelTransform.

   Camera is positioned at (0, 0, 3.2) looking toward -z. +y is up, +x is right.
   The mesh is auto-centered and auto-fit to a unit-sphere before any of these
   transforms are applied, so values are intuitive and small in magnitude.
   --------------------------------------------------------------------------- */
const DEFAULT_MODEL_TRANSFORM = {
  // Rotation in radians, BAKED INTO THE GEOMETRY (so the slow auto-spin still
  // rotates the mesh in place around world-Y after this re-orientation).
  // Useful when the source PLY is sideways or upside-down.
  rotation: { x: 1.0 * Math.PI, y: 0, z: 0 },
  // Uniform scale multiplier applied on top of the auto-fit. 1.0 = the mesh
  // fills a unit sphere; use <1 to shrink, >1 to enlarge.
  scale: 3.0,
  // Translation in world units, BAKED INTO THE GEOMETRY so the rotation
  // pivot stays fixed at the world origin (in front of the camera). Offsetting
  // the vertices off the pivot makes the mesh orbit around the pivot during
  // auto-spin, rather than rotating in place — a useful tweak for asymmetric
  // models or framing the mesh off-center.
  offset: { x: 0.0, y: -0.78, z: 1.08 },
};

function initHeroMesh(canvas, opts = {}) {
  if (!canvas) return null;

  // Reuse cached state across init/destroy cycles to avoid re-fetching PLY
  const cache = (initHeroMesh._cache = initHeroMesh._cache || {});

  // Resolve the model transform: defaults can be overridden per call.
  const userTx = opts.modelTransform || {};
  const modelTransform = {
    rotation: {
      ...DEFAULT_MODEL_TRANSFORM.rotation,
      ...(userTx.rotation || {}),
    },
    scale: userTx.scale ?? DEFAULT_MODEL_TRANSFORM.scale,
    offset: {
      ...DEFAULT_MODEL_TRANSFORM.offset,
      ...(userTx.offset || {}),
    },
  };

  // ---------- Mobile budget ----------
  // On phones the hero stutters because every frame re-renders a textured
  // mesh + a big additive-blended sparkle cloud into a high-DPR buffer.
  // Fragment cost scales with DPR², and transparent overdraw is the worst
  // case for mobile GPUs, so we dial those back on small viewports only.
  // Desktop keeps the full-quality settings untouched.
  const isMobile = !!(window.matchMedia &&
    window.matchMedia('(max-width: 820px)').matches);
  const PIXEL_RATIO = Math.min(window.devicePixelRatio || 1, isMobile ? 1.25 : 2);
  // Render at the display's native refresh rate (no throttle): a capped
  // frame rate read as choppier than the lighter full-rate loop.
  const MIN_FRAME_MS = 0;

  // ---------- Renderer / scene ----------
  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({
      canvas,
      antialias: !isMobile,
      alpha: true,
      powerPreference: 'high-performance',
    });
  } catch (e) {
    console.warn('[hero-mesh] WebGL unavailable:', e);
    return null;
  }
  renderer.setPixelRatio(PIXEL_RATIO);
  renderer.setClearColor(0x000000, 0);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  console.info('[hero-mesh] WebGL renderer ready');

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(32, 1, 0.01, 100);
  camera.position.set(0, 0, 3.2);

  // ---------- Lights ----------
  scene.add(new THREE.AmbientLight(0xffffff, 0.55));

  const keyLight = new THREE.DirectionalLight(0xffffff, 1.1);
  keyLight.position.set(2.5, 3, 4);
  scene.add(keyLight);

  // Accent rim from behind — green halo
  const rimLight = new THREE.DirectionalLight(ACCENT, 1.6);
  rimLight.position.set(-3, 0.5, -2);
  scene.add(rimLight);

  // Soft cyan fill
  const fillLight = new THREE.DirectionalLight(0x7fffea, 0.5);
  fillLight.position.set(0, -2, 1);
  scene.add(fillLight);

  // ---------- Motion preference ----------
  const reduceMotion = window.matchMedia &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ---------- Mesh group (rotation pivot) ----------
  const group = new THREE.Group();
  scene.add(group);

  let mesh = null;
  let edgeOverlay = null;
  let points = null;
  let revealAnim = null;
  let sparkle = null;
  let sparkleElapsedMs = 0;
  // Mobile-only lightweight effect: a static particle cloud that simply
  // cross-fades with the mesh (no per-vertex wave/twinkle shader).
  let switchPoints = null;
  let switchElapsedMs = 0;
  let destroyed = false;
  const EDGE_OPACITY = 0.12;
  const modelRotationOffset = new THREE.Euler(0, 0, 0);

  // ---- Sparkle / wave glow shaders -------------------------------------
  // Two Gaussian "waves" sweep along world-Y of the mesh, illuminating
  // points near them. A per-vertex twinkle modulates the brightness so
  // the surface never looks static. AdditiveBlending makes it read as
  // emission on top of the textured mesh.
  // The wave's vertical position and the global background-particle level
  // are both driven by the JS frame loop via uWavePhase / uBgIntensity, so
  // the GPU shader stays simple and we can sync mesh.material.opacity to
  // the same cycle without any guessing.
  const SPARKLE_VERT = /* glsl */`
    uniform float uTime;
    uniform float uMinY;
    uniform float uMaxY;
    uniform float uWaveWidth;
    uniform float uWavePhase;     // 0..1, wave position within the current pulse
    uniform float uBgIntensity;   // 0 = mesh mode, 1 = full particle mode
    uniform float uPixelRatio;
    attribute float aSeed;
    varying float vIntensity;

    void main() {
      float range = uMaxY - uMinY;
      float padded = range + uWaveWidth * 6.0;
      float wavePos = uMinY - uWaveWidth * 3.0 + uWavePhase * padded;

      float y = position.y;
      float d1 = (y - wavePos) / uWaveWidth;
      float w1 = exp(-d1 * d1);

      // Per-vertex twinkle so the particle "boot screen" mode has the same
      // alive feel as the assembly reveal.
      float twinkle = 0.6 + 0.4 * sin(uTime * 1.8 + aSeed * 28.0);

      // Background particles: visible in proportion to uBgIntensity. In
      // mesh mode (uBgIntensity = 0) the mesh dominates and only the wave
      // shows through. In particle mode (uBgIntensity = 1) every vertex is
      // bright and twinkly — same look as the initial reveal.
      float bg = uBgIntensity * (0.45 + 0.45 * twinkle);
      // Wave bump: a moving bright spot that runs once per pulse,
      // independent of which mode we're heading to/from.
      float intensity = (bg + w1 * 0.6) * 0.95;
      vIntensity = intensity;

      vec4 mv = modelViewMatrix * vec4(position, 1.0);
      // Tuned to match the small particle size of the initial assembly
      // reveal (PointsMaterial size 0.022 with sizeAttenuation): ~5–6 px
      // at typical particle-mode intensity, ~8–9 px at the wave peak.
      float baseSize = 0.005 + 0.05 * intensity;
      gl_PointSize = baseSize * uPixelRatio * (260.0 / -mv.z);
      gl_Position = projectionMatrix * mv;
    }
  `;

  const SPARKLE_FRAG = /* glsl */`
    uniform sampler2D uSprite;
    uniform vec3 uColor;
    varying float vIntensity;

    void main() {
      vec4 tex = texture2D(uSprite, gl_PointCoord);
      float a = tex.a * vIntensity;
      if (a < 0.003) discard;
      // Lower color boost — particles are tinted glow, not blown-out highlights.
      gl_FragColor = vec4(uColor * tex.rgb * (0.7 + vIntensity * 0.6), a);
    }
  `;

  function createSparkleLayer(srcGeometry) {
    if (sparkle || reduceMotion) return;
    const src = srcGeometry.attributes.position.array;
    const N_total = srcGeometry.attributes.position.count;
    // Cap the sparkle layer well below the mesh's vertex count — 35K
    // points is plenty for a dense glow without taxing weak GPUs. On phones
    // this additive cloud is the dominant per-frame cost, so cut it hard.
    const MAX = isMobile ? 11000 : 35000;
    const stride = Math.max(1, Math.floor(N_total / MAX));
    const N = Math.floor(N_total / stride);

    const posArr = new Float32Array(N * 3);
    const seedArr = new Float32Array(N);
    let minY = Infinity, maxY = -Infinity;
    for (let i = 0; i < N; i++) {
      const s = i * stride * 3;
      const x = src[s], y = src[s + 1], z = src[s + 2];
      posArr[i * 3]     = x;
      posArr[i * 3 + 1] = y;
      posArr[i * 3 + 2] = z;
      seedArr[i] = Math.random();
      if (y < minY) minY = y;
      if (y > maxY) maxY = y;
    }
    if (!isFinite(minY) || !isFinite(maxY) || maxY <= minY) return;

    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(posArr, 3));
    geo.setAttribute('aSeed',    new THREE.BufferAttribute(seedArr, 1));

    const range = maxY - minY;
    const mat = new THREE.ShaderMaterial({
      uniforms: {
        uTime:        { value: 0 },
        uMinY:        { value: minY },
        uMaxY:        { value: maxY },
        // Wave thickness as a fraction of the mesh height
        uWaveWidth:   { value: range * 0.18 },
        // Driven by the JS frame loop (see frame()):
        uWavePhase:   { value: 0 }, // 0..1 within the current pulse
        uBgIntensity: { value: 0 }, // 0 = mesh mode, 1 = particle mode
        uPixelRatio:  { value: PIXEL_RATIO },
        uColor:       { value: new THREE.Color(0x9affec) },
        uSprite:      { value: makeParticleSprite() },
      },
      vertexShader: SPARKLE_VERT,
      fragmentShader: SPARKLE_FRAG,
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    });

    sparkle = new THREE.Points(geo, mat);
    group.add(sparkle);
    sparkleElapsedMs = 0;
    // Make the mesh material support opacity animation. We drive its
    // opacity from the frame loop in lockstep with uBgIntensity so the
    // mesh fades out exactly while the particles fade in (and vice versa).
    if (mesh) {
      mesh.material.transparent = true;
      mesh.material.opacity = 1;
      mesh.material.needsUpdate = true;
    }
    console.info('[hero-mesh] sparkle layer ready —', N, 'points');
  }

  // ---- Mobile switch layer ---------------------------------------------
  // A static, non-animated particle cloud (positions never change, plain
  // additive sprites — no wave/twinkle shader). The frame loop just
  // cross-fades its opacity with the mesh's, and hides whichever object is
  // invisible so we never pay for both at once. Far lighter than the
  // sparkle layer while keeping a bit of life in the mobile hero.
  function createSwitchLayer(srcGeometry) {
    if (switchPoints || reduceMotion) return;
    const src = srcGeometry.attributes.position.array;
    const N_total = srcGeometry.attributes.position.count;
    const MAX = 12000;
    const stride = Math.max(1, Math.floor(N_total / MAX));
    const N = Math.floor(N_total / stride);

    const posArr = new Float32Array(N * 3);
    for (let i = 0; i < N; i++) {
      const s = i * stride * 3;
      posArr[i * 3]     = src[s];
      posArr[i * 3 + 1] = src[s + 1];
      posArr[i * 3 + 2] = src[s + 2];
    }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(posArr, 3));
    const mat = new THREE.PointsMaterial({
      size: 0.02,
      map: makeParticleSprite(),
      color: 0xc8fff0,
      transparent: true,
      opacity: 0,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
      sizeAttenuation: true,
    });
    switchPoints = new THREE.Points(geo, mat);
    switchPoints.visible = false;
    group.add(switchPoints);
    switchElapsedMs = 0;
    if (mesh) {
      mesh.material.transparent = true;
      mesh.material.opacity = 1;
      mesh.material.needsUpdate = true;
    }
    console.info('[hero-mesh] mobile switch layer ready —', N, 'points');
  }

  // If the PLY ships with per-vertex colors, render unlit so the texture
  // reads as an emissive RGB surface (matches the gallery viewers' look).
  // Otherwise fall back to the brand-green flat-shaded Phong material.
  function makeMaterial(hasVertexColors) {
    if (hasVertexColors) {
      return new THREE.MeshBasicMaterial({
        vertexColors: true,
        toneMapped: false,
      });
    }
    return new THREE.MeshPhongMaterial({
      color: ACCENT,
      specular: 0xffffff,
      shininess: 35,
      emissive: ACCENT,
      emissiveIntensity: 0.18,
      flatShading: true,
    });
  }

  // Soft circular green sprite for the assembly particles
  let particleTexture = null;
  function makeParticleSprite() {
    if (particleTexture) return particleTexture;
    const size = 64;
    const c = document.createElement('canvas');
    c.width = c.height = size;
    const ctx = c.getContext('2d');
    const r = size / 2;
    const grad = ctx.createRadialGradient(r, r, 0, r, r, r);
    grad.addColorStop(0.0, 'rgba(200, 255, 245, 1)');
    grad.addColorStop(0.3, 'rgba(77, 141, 255, 0.85)');
    grad.addColorStop(0.7, 'rgba(77, 141, 255, 0.18)');
    grad.addColorStop(1.0, 'rgba(77, 141, 255, 0)');
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, size, size);
    particleTexture = new THREE.CanvasTexture(c);
    particleTexture.colorSpace = THREE.SRGBColorSpace;
    return particleTexture;
  }

  // Build a Points cloud whose targets are the mesh vertex positions, but
  // whose initial positions are randomly scattered on a sphere shell. Returns
  // an animation state used by tickReveal().
  function setupParticleReveal(geometry) {
    const fullPos = geometry.attributes.position.array;
    const N_total = geometry.attributes.position.count;
    // Cap to keep the assembly silky-smooth even on integrated GPUs.
    // The reveal updates every particle's position on the CPU each frame,
    // so a lower budget on phones removes the initial stutter too.
    const MAX_PARTICLES = isMobile ? 15000 : 60000;
    const stride = Math.max(1, Math.floor(N_total / MAX_PARTICLES));
    const N = Math.floor(N_total / stride);

    const finalArr = new Float32Array(N * 3);
    const startArr = new Float32Array(N * 3);
    const curArr = new Float32Array(N * 3);

    for (let i = 0; i < N; i++) {
      const s = i * stride * 3;
      finalArr[i * 3]     = fullPos[s];
      finalArr[i * 3 + 1] = fullPos[s + 1];
      finalArr[i * 3 + 2] = fullPos[s + 2];

      // Random direction on a sphere, radius 2.0–3.6
      const u = Math.random(), v = Math.random();
      const theta = 2 * Math.PI * u;
      const phi = Math.acos(2 * v - 1);
      const rr = 2.0 + Math.random() * 1.6;
      startArr[i * 3]     = rr * Math.sin(phi) * Math.cos(theta);
      startArr[i * 3 + 1] = rr * Math.sin(phi) * Math.sin(theta);
      startArr[i * 3 + 2] = rr * Math.cos(phi);
    }
    curArr.set(startArr);

    const pgeo = new THREE.BufferGeometry();
    pgeo.setAttribute('position', new THREE.BufferAttribute(curArr, 3));
    const pmat = new THREE.PointsMaterial({
      size: 0.022,
      map: makeParticleSprite(),
      color: 0xc8fff0,
      transparent: true,
      opacity: 0,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
      sizeAttenuation: true,
    });
    points = new THREE.Points(pgeo, pmat);
    group.add(points);

    // Hide the lit mesh and edges while particles assemble
    mesh.material.transparent = true;
    mesh.material.opacity = 0;
    mesh.material.needsUpdate = true;
    if (edgeOverlay) edgeOverlay.material.opacity = 0;

    revealAnim = {
      elapsed: 0,
      fadeInDur: 280,
      gatherDur: 1700,
      crossfadeDur: 700,
      N,
      startArr,
      curArr,
      finalArr,
      pgeo,
      pmat,
    };
    console.info('[hero-mesh] particle reveal armed —', N, 'particles');
  }

  function tickReveal(dt) {
    if (!revealAnim) return;
    const a = revealAnim;
    a.elapsed += dt;
    const t = a.elapsed;

    // 1) Gather: ease-out quartic from scattered → final
    const gT = Math.min(1, t / a.gatherDur);
    const eG = 1 - Math.pow(1 - gT, 4);
    const sa = a.startArr, fa = a.finalArr, ca = a.curArr;
    for (let i = 0; i < ca.length; i++) {
      ca[i] = sa[i] + (fa[i] - sa[i]) * eG;
    }
    a.pgeo.attributes.position.needsUpdate = true;

    // 2) Particle alpha: fade in fast, then cross-fade out at the end
    const fIn = Math.min(1, t / a.fadeInDur);
    let pAlpha = fIn;
    let mAlpha = 0;
    if (t > a.gatherDur) {
      const cf = Math.min(1, (t - a.gatherDur) / a.crossfadeDur);
      pAlpha = 1 - cf;
      mAlpha = cf;
    }
    a.pmat.opacity = pAlpha;
    if (mesh) mesh.material.opacity = mAlpha;
    if (edgeOverlay) edgeOverlay.material.opacity = EDGE_OPACITY * mAlpha;

    // 3) Done: dispose the particle system, leave the mesh fully opaque
    if (t >= a.gatherDur + a.crossfadeDur) {
      group.remove(points);
      a.pgeo.dispose();
      a.pmat.dispose();
      points = null;
      if (mesh) {
        mesh.material.transparent = false;
        mesh.material.opacity = 1;
        mesh.material.needsUpdate = true;
      }
      if (edgeOverlay) edgeOverlay.material.opacity = EDGE_OPACITY;
      revealAnim = null;
      // Desktop: the full pulsing wave-glow sparkle layer.
      // Mobile: a lightweight static particle ↔ mesh cross-fade instead of
      // the per-frame animated additive cloud.
      if (mesh) {
        if (isMobile) createSwitchLayer(mesh.geometry);
        else createSparkleLayer(mesh.geometry);
      }
    }
  }

  function buildMesh(geometry) {
    if (destroyed) {
      try { geometry.dispose(); } catch (e) {}
      return;
    }
    geometry.computeBoundingBox();
    const bbox = geometry.boundingBox;
    const center = bbox.getCenter(new THREE.Vector3());
    const size = bbox.getSize(new THREE.Vector3());
    const maxDim = Math.max(size.x, size.y, size.z) || 1;

    // 1. Auto-center at origin
    geometry.translate(-center.x, -center.y, -center.z);

    // 2. Auto-fit to unit sphere, then apply user scale multiplier
    const targetSize = 1.6;
    const autoScale = (targetSize / maxDim) * modelTransform.scale;
    geometry.scale(autoScale, autoScale, autoScale);

    // 3. Bake user rotation into the geometry so the auto-spin still spins
    //    around world-Y in place.
    const r = modelTransform.rotation;
    if (r.x) geometry.rotateX(r.x);
    if (r.y) geometry.rotateY(r.y);
    if (r.z) geometry.rotateZ(r.z);

    // 4. Bake user offset into the geometry too — the rotation pivot stays at
    //    the world origin so the mesh orbits around it instead of moving with it.
    const o = modelTransform.offset;
    if (o.x || o.y || o.z) geometry.translate(o.x, o.y, o.z);

    // Recompute normals AFTER all geometry transforms so lighting is correct
    geometry.computeVertexNormals();

    const hasVertexColors = !!geometry.attributes.color;
    mesh = new THREE.Mesh(geometry, makeMaterial(hasVertexColors));
    group.add(mesh);

    // Subtle accent edge overlay so silhouette reads even at low light.
    // Skipped for textured meshes — the green wireframe muddies RGB colors.
    if (!hasVertexColors) {
      try {
        const edges = new THREE.EdgesGeometry(geometry, 30);
        const edgeMat = new THREE.LineBasicMaterial({
          color: ACCENT,
          transparent: true,
          opacity: EDGE_OPACITY,
        });
        edgeOverlay = new THREE.LineSegments(edges, edgeMat);
        group.add(edgeOverlay);
      } catch (e) { /* optional */ }
    }

    group.rotation.set(0, 0, 0);
    modelRotationOffset.set(0, 0, 0);
    console.info('[hero-mesh] PLY mesh ready —', geometry.attributes.position.count, 'verts');

    // Kick off the particle-assembly reveal (skipped under reduced-motion)
    if (!reduceMotion) setupParticleReveal(geometry);
    if (typeof opts.onReady === 'function') opts.onReady();
  }

  function loadPly() {
    const url = opts.url || './assets/mesh_minimal_decimated02.ply';
    // Cache geometry PER URL so multiple viewers on the same page can share
    // an already-fetched PLY without stomping on each other's data.
    if (cache[url]) {
      buildMesh(cache[url].clone());
      return;
    }
    const loader = new PLYLoader();
    console.info('[hero-mesh] loading', url);
    loader.load(
      url,
      (geometry) => {
        cache[url] = geometry;
        buildMesh(geometry.clone());
      },
      (xhr) => {
        if (xhr && xhr.lengthComputable) {
          const pct = Math.round((xhr.loaded / xhr.total) * 100);
          if (pct % 25 === 0) console.info('[hero-mesh] ' + url.split('/').pop() + ' ' + pct + '%');
        }
      },
      (err) => {
        if (destroyed) return;
        console.error('[hero-mesh] failed to load PLY:', url, err);
        if (typeof opts.onError === 'function') opts.onError(err);
      }
    );
  }
  loadPly();

  // ---------- Resize ----------
  function resize() {
    const parent = canvas.parentElement || canvas;
    const w = parent.clientWidth || window.innerWidth || 800;
    const h = parent.clientHeight || window.innerHeight || 600;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }
  resize();
  let ro;
  if ('ResizeObserver' in window) {
    ro = new ResizeObserver(resize);
    ro.observe(canvas.parentElement || canvas);
  } else {
    window.addEventListener('resize', resize);
  }

  // ---------- Pointer parallax ----------
  let pointerX = 0, pointerY = 0;
  let targetPX = 0, targetPY = 0;

  function onPointerMove(e) {
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    targetPX = ((e.clientX - rect.left) / rect.width - 0.5) * 2;
    targetPY = ((e.clientY - rect.top) / rect.height - 0.5) * 2;
  }
  function onPointerLeave() { targetPX = 0; targetPY = 0; }
  // Pointer parallax is a hover effect — pointless on touch, and the
  // getBoundingClientRect() read on every move would fire during scroll.
  // Skip it entirely on phones.
  if (!isMobile) {
    window.addEventListener('pointermove', onPointerMove, { passive: true });
    window.addEventListener('pointerleave', onPointerLeave);
  }

  // ---------- Visibility (pause off-screen) ----------
  let visible = true;
  let io;
  if ('IntersectionObserver' in window) {
    io = new IntersectionObserver(
      ([entry]) => { visible = entry.isIntersecting; },
      { threshold: 0.01 }
    );
    io.observe(canvas);
  }
  function onVisChange() {
    if (document.hidden) visible = false;
    else if (io) {
      // Recompute on next IO tick
      const rect = canvas.getBoundingClientRect();
      visible = rect.bottom > 0 && rect.top < window.innerHeight;
    }
  }
  document.addEventListener('visibilitychange', onVisChange);

  // ---------- Animation loop ----------
  const autoRotate = reduceMotion ? 0 : (opts.autoRotate ?? 0.00045);

  let running = true;
  let last = performance.now();
  function frame(now) {
    if (!running) return;
    requestAnimationFrame(frame);
    if (!visible) { last = now; return; }
    // Frame-rate throttle (mobile only): skip this rAF tick until at least
    // MIN_FRAME_MS has elapsed. `last` is left untouched so the next dt
    // covers the full skipped interval and motion stays time-correct.
    if (MIN_FRAME_MS && (now - last) < MIN_FRAME_MS) return;
    const dt = Math.min(50, now - last);
    last = now;

    // Smooth pointer
    pointerX += (targetPX - pointerX) * 0.05;
    pointerY += (targetPY - pointerY) * 0.05;

    // Continuous spin + parallax tilt. The user offset is baked into the
    // geometry, so the rotation pivot stays at the world origin (only the
    // small parallax drift moves the group itself).
    modelRotationOffset.y += autoRotate * dt;
    group.rotation.y = modelRotationOffset.y + pointerX * 0.35;
    group.rotation.x = pointerY * 0.22;
    group.position.x = pointerX * 0.04;
    group.position.y = -pointerY * 0.02;
    group.position.z = 0;

    // Particle-assembly reveal (no-op once finished). Uses accumulated visible
    // dt so that pausing while off-screen / in lite mode doesn't fast-forward
    // through the animation.
    tickReveal(dt);

    // Sparkle / wave-pulse layer (only present after the reveal completes).
    // Each pulse switches the visual mode: mesh ↔ particles, with the wave
    // sweeping vertically as the transition front. Between pulses we hold
    // the destination mode for a moment so each state is clearly readable.
    if (sparkle && mesh) {
      sparkleElapsedMs += dt;
      const elapsed = sparkleElapsedMs * 0.001;
      const u = sparkle.material.uniforms;
      u.uTime.value = elapsed;

      const PULSE_DURATION = 0.75; // seconds spent transitioning
      const HOLD_DURATION  = 2.0; // seconds spent holding the destination mode
      const CYCLE_DURATION = PULSE_DURATION + HOLD_DURATION;

      const cycleNum  = Math.floor(elapsed / CYCLE_DURATION);
      const cycleTime = elapsed - cycleNum * CYCLE_DURATION;

      // pulsePhase ramps 0 → 1 over PULSE_DURATION, then stays clamped at
      // 1 for HOLD_DURATION while we hold the new mode (wave is parked
      // above the mesh, so it's invisible during the hold).
      const pulsePhase = Math.min(1.0, cycleTime / PULSE_DURATION);

      u.uWavePhase.value = pulsePhase;

      // Smoothstep ease for the mode crossfade (the wave itself moves
      // linearly so it reads as a steady sweep).
      const ts = pulsePhase * pulsePhase * (3.0 - 2.0 * pulsePhase);

      // Even cycles start in mesh mode, odd cycles start in particle mode.
      // After each pulse, the active mode flips — exactly the user's spec.
      const startInMesh = (cycleNum % 2 === 0);
      const meshOp = startInMesh ? (1.0 - ts) : ts;

      mesh.material.opacity = meshOp;
      u.uBgIntensity.value = 1.0 - meshOp;
    }

    // Mobile switch layer: hold mesh, fade to particles, hold particles,
    // fade back — a plain opacity cross-fade with no per-vertex animation.
    // Whichever object is invisible is also culled so we never render both.
    if (switchPoints && mesh) {
      switchElapsedMs += dt;
      const t = switchElapsedMs * 0.001;
      const FADE = 0.8;   // seconds spent cross-fading
      const HOLD = 2.4;   // seconds spent holding each state
      const FULL = 2 * (FADE + HOLD);
      const tt = t % FULL;

      let p; // particle-cloud opacity (0 = mesh, 1 = particles)
      if (tt < HOLD) {
        p = 0;
      } else if (tt < HOLD + FADE) {
        const x = (tt - HOLD) / FADE;
        p = x * x * (3 - 2 * x);
      } else if (tt < 2 * HOLD + FADE) {
        p = 1;
      } else {
        const x = (tt - (2 * HOLD + FADE)) / FADE;
        p = 1 - x * x * (3 - 2 * x);
      }

      const meshOp = 1 - p;
      mesh.material.opacity = meshOp;
      mesh.visible = meshOp > 0.003;
      switchPoints.material.opacity = p;
      switchPoints.visible = p > 0.003;
      if (edgeOverlay) {
        edgeOverlay.material.opacity = EDGE_OPACITY * meshOp;
        edgeOverlay.visible = meshOp > 0.003;
      }
    }

    renderer.render(scene, camera);
  }
  requestAnimationFrame(frame);

  // ---------- Public ----------
  return {
    destroy() {
      running = false;
      destroyed = true;
      try { if (ro) ro.disconnect(); } catch (e) {}
      try { if (io) io.disconnect(); } catch (e) {}
      window.removeEventListener('pointermove', onPointerMove);
      window.removeEventListener('pointerleave', onPointerLeave);
      window.removeEventListener('resize', resize);
      document.removeEventListener('visibilitychange', onVisChange);

      if (mesh) {
        try { mesh.geometry.dispose(); } catch (e) {}
        try { mesh.material.dispose(); } catch (e) {}
      }
      if (edgeOverlay) {
        try { edgeOverlay.geometry.dispose(); } catch (e) {}
        try { edgeOverlay.material.dispose(); } catch (e) {}
      }
      if (points) {
        try { points.geometry.dispose(); } catch (e) {}
        try { points.material.dispose(); } catch (e) {}
      }
      if (sparkle) {
        try { sparkle.geometry.dispose(); } catch (e) {}
        try { sparkle.material.dispose(); } catch (e) {}
        sparkle = null;
      }
      if (switchPoints) {
        try { switchPoints.geometry.dispose(); } catch (e) {}
        try { switchPoints.material.dispose(); } catch (e) {}
        switchPoints = null;
      }
      if (particleTexture) {
        try { particleTexture.dispose(); } catch (e) {}
        particleTexture = null;
      }
      revealAnim = null;
      // Note: deliberately NOT calling WEBGL_lose_context.loseContext() here.
      // The canvas element is reused on lite-mode toggle, and forcing context
      // loss can leave the next WebGLRenderer in a degraded state on some
      // browsers (e.g. the second reveal would never play).
      try { renderer.dispose(); } catch (e) {}
    }
  };
}

window.initHeroMesh = initHeroMesh;
