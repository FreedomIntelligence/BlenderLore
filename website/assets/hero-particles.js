/* =========================================================
   hero-particles.js
   Fullscreen animated 3D point cloud (Gaussian-splat aesthetic).

   Performance-optimized:
   - Pre-rendered radial-gradient sprite (one createRadialGradient
     for the whole lifetime of the page — vs. ~1500/frame before).
   - drawImage stamping instead of per-particle arc + gradient.
   - No back-to-front sort: with 'lighter' blending, order is
     visually irrelevant.
   - Optional FPS-adaptive density.

   Public API: window.initHeroParticles(canvas, options) -> { start, stop, destroy }
   ========================================================= */
(function () {
  'use strict';

  function initHeroParticles(canvas, options) {
    if (!canvas) return null;
    const opts = Object.assign({
      pointCount: 1200,
      shell: 'sphere',          // 'sphere' | 'torus'
      autoRotate: 0.00012,      // radians per ms (Y axis)
      tiltAmplitude: 0.35,      // mouse parallax tilt magnitude
      depthScale: 0.42,         // perspective spread
      baseSize: 1.7,            // base disc radius
      color: [94, 224, 214],    // accent rgb
      density: 1.0,             // density multiplier (1 = full)
      adaptive: true,           // adjust point count if frame time spikes
    }, options || {});

    const ctx = canvas.getContext('2d', { alpha: true });
    if (!ctx) return null;

    const reduceMotion =
      window.matchMedia &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    let dpr = Math.min(window.devicePixelRatio || 1, 2);
    let width = 0, height = 0;
    let cx = 0, cy = 0;

    const mouse = { x: 0, y: 0, tx: 0, ty: 0 };

    // ---- Pre-render sprite (huge perf win) -------------------------------
    const SPRITE_SIZE = 64; // logical px; we'll scale per-particle on draw
    const sprite = document.createElement('canvas');
    sprite.width = SPRITE_SIZE;
    sprite.height = SPRITE_SIZE;
    {
      const sctx = sprite.getContext('2d');
      const cx2 = SPRITE_SIZE / 2;
      const r = SPRITE_SIZE / 2;
      const grad = sctx.createRadialGradient(cx2, cx2, 0, cx2, cx2, r);
      const [R, G, B] = opts.color;
      grad.addColorStop(0,    `rgba(${R},${G},${B},1)`);
      grad.addColorStop(0.18, `rgba(${R},${G},${B},0.72)`);
      grad.addColorStop(0.45, `rgba(${R},${G},${B},0.22)`);
      grad.addColorStop(1,    `rgba(${R},${G},${B},0)`);
      sctx.fillStyle = grad;
      sctx.fillRect(0, 0, SPRITE_SIZE, SPRITE_SIZE);
    }

    // ---- Build points ----------------------------------------------------
    let totalPoints = Math.max(200, Math.floor(opts.pointCount * opts.density));
    // Use parallel typed arrays to keep memory hot
    let px = new Float32Array(totalPoints);
    let py = new Float32Array(totalPoints);
    let pz = new Float32Array(totalPoints);
    let phase = new Float32Array(totalPoints);
    let speed = new Float32Array(totalPoints);

    function spawnPoints(n) {
      totalPoints = n;
      px = new Float32Array(n);
      py = new Float32Array(n);
      pz = new Float32Array(n);
      phase = new Float32Array(n);
      speed = new Float32Array(n);
      for (let i = 0; i < n; i++) {
        let x, y, z;
        if (opts.shell === 'torus') {
          const u = Math.random() * Math.PI * 2;
          const v = Math.random() * Math.PI * 2;
          const R = 1.0, r = 0.38;
          x = (R + r * Math.cos(v)) * Math.cos(u);
          y = r * Math.sin(v);
          z = (R + r * Math.cos(v)) * Math.sin(u);
        } else {
          const theta = Math.random() * Math.PI * 2;
          const phi = Math.acos(2 * Math.random() - 1);
          const radius = 0.78 + Math.random() * 0.34;
          x = radius * Math.sin(phi) * Math.cos(theta);
          y = radius * Math.sin(phi) * Math.sin(theta);
          z = radius * Math.cos(phi);
        }
        px[i] = x; py[i] = y; pz[i] = z;
        phase[i] = Math.random() * Math.PI * 2;
        speed[i] = 0.4 + Math.random() * 0.9;
      }
    }
    spawnPoints(totalPoints);

    // ---- Sizing ----------------------------------------------------------
    function resize() {
      const rect = canvas.getBoundingClientRect();
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      width = Math.max(1, Math.floor(rect.width));
      height = Math.max(1, Math.floor(rect.height));
      canvas.width = Math.floor(width * dpr);
      canvas.height = Math.floor(height * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      cx = width / 2;
      cy = height / 2;
    }
    resize();
    window.addEventListener('resize', resize, { passive: true });

    // ---- Mouse parallax --------------------------------------------------
    function onMouse(e) {
      const rect = canvas.getBoundingClientRect();
      const nx = ((e.clientX - rect.left) / rect.width) * 2 - 1;
      const ny = ((e.clientY - rect.top) / rect.height) * 2 - 1;
      mouse.tx = Math.max(-1, Math.min(1, nx));
      mouse.ty = Math.max(-1, Math.min(1, ny));
    }
    function onLeave() { mouse.tx = 0; mouse.ty = 0; }
    window.addEventListener('mousemove', onMouse, { passive: true });
    window.addEventListener('mouseleave', onLeave, { passive: true });

    // ---- Render loop -----------------------------------------------------
    let running = false;
    let rafId = 0;
    let lastT = 0;
    let yaw = 0;

    // FPS-adaptive throttling
    let avgFrameMs = 16.67;
    let lastAdjustT = 0;

    function frame(t) {
      if (!running) return;
      if (!lastT) lastT = t;
      const dt = Math.min(64, t - lastT);
      lastT = t;

      // Smooth running average (EMA)
      avgFrameMs = avgFrameMs * 0.92 + dt * 0.08;

      // Adapt point count if persistently slow (every ~2s)
      if (opts.adaptive && t - lastAdjustT > 2000) {
        lastAdjustT = t;
        if (avgFrameMs > 28 && totalPoints > 300) {
          spawnPoints(Math.floor(totalPoints * 0.75));
        } else if (avgFrameMs < 17 && totalPoints < opts.pointCount) {
          spawnPoints(Math.min(opts.pointCount, Math.floor(totalPoints * 1.15)));
        }
      }

      mouse.x += (mouse.tx - mouse.x) * 0.05;
      mouse.y += (mouse.ty - mouse.y) * 0.05;

      if (!reduceMotion) {
        yaw += opts.autoRotate * dt;
      }

      const tiltX = mouse.y * opts.tiltAmplitude;
      const tiltY = mouse.x * opts.tiltAmplitude;

      const cosY = Math.cos(yaw + tiltY);
      const sinY = Math.sin(yaw + tiltY);
      const cosX = Math.cos(tiltX);
      const sinX = Math.sin(tiltX);

      const projW = Math.min(width, height) * opts.depthScale;
      const baseR = opts.baseSize * Math.max(1, Math.min(width, height) / 900);

      // Clear
      ctx.globalCompositeOperation = 'source-over';
      ctx.clearRect(0, 0, width, height);

      // Additive stamping
      ctx.globalCompositeOperation = 'lighter';

      const tSec = t * 0.001;
      // Cull margin so off-screen points are skipped
      const margin = 32;

      for (let i = 0; i < totalPoints; i++) {
        // rotate around Y
        const x0 = px[i], y0 = py[i], z0 = pz[i];
        const x1 = x0 * cosY + z0 * sinY;
        const z1 = -x0 * sinY + z0 * cosY;
        // rotate around X (pitch)
        const y2 = y0 * cosX - z1 * sinX;
        const z2 = y0 * sinX + z1 * cosX;

        const persp = 1 / (2.2 + z2);
        const sx = cx + x1 * persp * projW;
        const sy = cy + y2 * persp * projW;

        if (sx < -margin || sx > width + margin || sy < -margin || sy > height + margin) continue;

        const depth = (z2 + 1.2) / 2.4; // 0..~1
        const tw = 0.85 + 0.15 * Math.sin(phase[i] + tSec * speed[i]);
        const a = (1 - depth) * 0.85 * tw;
        if (a <= 0.02) continue;

        // Particle "halo" radius (sprite is drawn at this size)
        const halo = baseR * persp * (0.6 + (1 - depth) * 1.1) * 6.4;
        if (halo < 1.2) continue;

        ctx.globalAlpha = Math.min(1, a);
        ctx.drawImage(sprite, sx - halo * 0.5, sy - halo * 0.5, halo, halo);
      }

      ctx.globalAlpha = 1;
      ctx.globalCompositeOperation = 'source-over';

      if (!reduceMotion) {
        rafId = requestAnimationFrame(frame);
      } else {
        running = false;
      }
    }

    function start() {
      if (running) return;
      running = true;
      lastT = 0;
      rafId = requestAnimationFrame(frame);
    }
    function stop() {
      running = false;
      if (rafId) cancelAnimationFrame(rafId);
      rafId = 0;
    }

    let io;
    if ('IntersectionObserver' in window) {
      io = new IntersectionObserver((entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) start();
          else stop();
        }
      }, { threshold: 0.01 });
      io.observe(canvas);
    } else {
      start();
    }

    // Reduced motion: render one static frame
    if (reduceMotion) {
      running = true;
      frame(performance.now());
      running = false;
    }

    return {
      start,
      stop,
      destroy() {
        stop();
        if (io) io.disconnect();
        window.removeEventListener('resize', resize);
        window.removeEventListener('mousemove', onMouse);
        window.removeEventListener('mouseleave', onLeave);
      }
    };
  }

  window.initHeroParticles = initHeroParticles;
})();
