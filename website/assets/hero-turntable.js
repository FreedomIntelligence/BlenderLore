import * as THREE from 'three';

function initHeroTurntable(canvas, opts = {}) {
  const columns = Math.max(1, opts.columns ?? 6);
  const rows = Math.max(1, opts.rows ?? 6);
  const frameCount = Math.max(1, opts.frameCount ?? columns * rows);
  const atlasUrl = opts.atlasUrl;
  if (!canvas || !atlasUrl) return null;

  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({
      canvas,
      alpha: true,
      antialias: true,
      powerPreference: 'high-performance',
    });
  } catch (error) {
    opts.onError?.(error);
    return null;
  }
  renderer.setClearColor(0x000000, 0);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.NoToneMapping;
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));

  const scene = new THREE.Scene();
  const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, -2, 2);
  camera.position.z = 1;

  const geometry = new THREE.PlaneGeometry(2, 2);
  const material = new THREE.MeshBasicMaterial({
    transparent: true,
    depthTest: false,
    depthWrite: false,
    toneMapped: false,
    side: THREE.DoubleSide,
  });
  const plane = new THREE.Mesh(geometry, material);
  const baseScale = opts.scale ?? 0.93;
  plane.visible = false;
  plane.position.y = opts.offsetY ?? 0.02;
  plane.scale.setScalar(baseScale);
  scene.add(plane);

  const loader = new THREE.TextureLoader();
  let atlasTexture = null;
  let destroyed = false;
  let running = true;
  let visible = true;
  let readySent = false;
  let frameCursor = opts.initialFrame ?? 0;
  let displayedFrame = -1;
  let dragging = false;
  let pointerId = null;
  let dragStartX = 0;
  let dragStartFrame = 0;
  let lastTime = performance.now();
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  loader.load(
    atlasUrl,
    (texture) => {
      if (destroyed) {
        texture.dispose();
        return;
      }
      texture.colorSpace = THREE.SRGBColorSpace;
      texture.wrapS = THREE.ClampToEdgeWrapping;
      texture.wrapT = THREE.ClampToEdgeWrapping;
      texture.generateMipmaps = false;
      texture.minFilter = THREE.LinearFilter;
      texture.magFilter = THREE.LinearFilter;
      texture.repeat.set(1 / columns, 1 / rows);
      atlasTexture = texture;
      material.map = texture;
      material.needsUpdate = true;
      plane.visible = true;
      readySent = true;
      opts.onReady?.();
    },
    undefined,
    () => {
      if (readySent) return;
      readySent = true;
      opts.onError?.();
    },
  );

  function resize() {
    const width = canvas.clientWidth || 1;
    const height = canvas.clientHeight || 1;
    renderer.setSize(width, height, false);
    const aspect = width / height;
    camera.left = -aspect;
    camera.right = aspect;
    camera.top = 1;
    camera.bottom = -1;
    camera.updateProjectionMatrix();
    plane.scale.setScalar(baseScale * Math.min(1, aspect / 0.72));
  }

  resize();
  const resizeObserver = 'ResizeObserver' in window ? new ResizeObserver(resize) : null;
  resizeObserver?.observe(canvas);
  if (!resizeObserver) window.addEventListener('resize', resize);

  const intersectionObserver = new IntersectionObserver(
    (entries) => {
      visible = entries[0]?.isIntersecting ?? true;
      lastTime = performance.now();
    },
    { threshold: 0.01 },
  );
  intersectionObserver.observe(canvas);

  function onPointerDown(event) {
    if (event.pointerType === 'mouse' && event.button !== 0) return;
    dragging = true;
    pointerId = event.pointerId;
    dragStartX = event.clientX;
    dragStartFrame = frameCursor;
    canvas.setPointerCapture?.(pointerId);
    canvas.style.cursor = 'grabbing';
  }

  function onPointerMove(event) {
    if (!dragging || event.pointerId !== pointerId) return;
    frameCursor = dragStartFrame - (event.clientX - dragStartX) / (opts.pixelsPerFrame ?? 9);
  }

  function onPointerUp(event) {
    if (!dragging || (event.pointerId !== undefined && event.pointerId !== pointerId)) return;
    if (canvas.hasPointerCapture?.(pointerId)) canvas.releasePointerCapture(pointerId);
    dragging = false;
    pointerId = null;
    canvas.style.cursor = 'grab';
  }

  canvas.style.cursor = 'grab';
  canvas.style.touchAction = 'none';
  canvas.addEventListener('pointerdown', onPointerDown);
  window.addEventListener('pointermove', onPointerMove);
  window.addEventListener('pointerup', onPointerUp);
  window.addEventListener('pointercancel', onPointerUp);

  function animate(now) {
    if (!running) return;
    requestAnimationFrame(animate);
    const deltaMs = Math.min(50, now - lastTime);
    lastTime = now;
    if (!visible) return;

    if (!dragging && !reduceMotion) {
      const radiansPerMs = opts.autoRotate ?? 0.00007;
      frameCursor += radiansPerMs * deltaMs * frameCount / (Math.PI * 2);
    }
    const normalized = ((Math.round(frameCursor) % frameCount) + frameCount) % frameCount;
    if (atlasTexture && normalized !== displayedFrame) {
      const column = normalized % columns;
      const row = Math.floor(normalized / columns);
      atlasTexture.offset.set(
        column / columns,
        (rows - row - 1) / rows,
      );
      displayedFrame = normalized;
    }
    renderer.render(scene, camera);
  }
  requestAnimationFrame(animate);

  return {
    destroy() {
      running = false;
      destroyed = true;
      resizeObserver?.disconnect();
      intersectionObserver.disconnect();
      if (!resizeObserver) window.removeEventListener('resize', resize);
      canvas.removeEventListener('pointerdown', onPointerDown);
      window.removeEventListener('pointermove', onPointerMove);
      window.removeEventListener('pointerup', onPointerUp);
      window.removeEventListener('pointercancel', onPointerUp);
      if (pointerId !== null && canvas.hasPointerCapture?.(pointerId)) {
        canvas.releasePointerCapture(pointerId);
      }
      canvas.style.cursor = '';
      canvas.style.touchAction = '';
      atlasTexture?.dispose();
      material.dispose();
      geometry.dispose();
      renderer.renderLists.dispose();
      renderer.dispose();
    },
  };
}

window.initHeroTurntable = initHeroTurntable;
