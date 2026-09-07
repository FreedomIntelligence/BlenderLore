import * as THREE from 'three';
import { GLTFLoader } from './vendor/three/loaders/GLTFLoader.js';
import { RoomEnvironment } from './vendor/three/environments/RoomEnvironment.js';
import { EffectComposer } from './vendor/three/postprocessing/EffectComposer.js';
import { RenderPass } from './vendor/three/postprocessing/RenderPass.js';
import { OutlinePass } from './vendor/three/postprocessing/OutlinePass.js';
import { UnrealBloomPass } from './vendor/three/postprocessing/UnrealBloomPass.js';
import { OutputPass } from './vendor/three/postprocessing/OutputPass.js';

const ACCENT = 0x4d8dff;

function initHeroGLB(canvas, opts = {}) {
  if (!canvas) return null;

  const interactive = opts.interactive !== false;
  const isMobile = !!(window.matchMedia
    && window.matchMedia('(max-width: 820px)').matches);
  const reduceMotion = !!(window.matchMedia
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches);

  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({
      canvas,
      antialias: !isMobile,
      alpha: true,
      preserveDrawingBuffer: !!opts.preserveDrawingBuffer,
      powerPreference: 'high-performance',
    });
  } catch (error) {
    console.warn('[hero-glb] WebGL unavailable:', error);
    return null;
  }

  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, isMobile ? 1.25 : 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 0.8;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;

  const scene = new THREE.Scene();
  scene.fog = opts.kind === 'maoqiu' ? null : new THREE.FogExp2(0x0b0c0e, 0.025);
  const camera = new THREE.PerspectiveCamera(38, 1, 0.01, 100);
  camera.position.set(0, 0, 3.4);

  const roomEnvironment = new RoomEnvironment();
  const pmrem = new THREE.PMREMGenerator(renderer);
  const environmentTarget = pmrem.fromScene(roomEnvironment, 0.04);
  scene.environment = environmentTarget.texture;
  roomEnvironment.dispose();
  pmrem.dispose();

  scene.add(new THREE.HemisphereLight(0xc8e9ff, 0x3a241a, 0.48));
  const keyLight = new THREE.DirectionalLight(0xfff0d5, 1.45);
  keyLight.position.set(5, 7, 8);
  keyLight.castShadow = true;
  keyLight.shadow.mapSize.set(isMobile ? 1024 : 2048, isMobile ? 1024 : 2048);
  scene.add(keyLight);
  const rimLight = new THREE.DirectionalLight(0x73d9ff, 1.15);
  rimLight.position.set(-7, 3, -5);
  scene.add(rimLight);
  const fillLight = new THREE.PointLight(0xff6fae, 0.55, 30);
  fillLight.position.set(-4, -1, 5);
  scene.add(fillLight);

  const pivot = new THREE.Group();
  const fitGroup = new THREE.Group();
  pivot.add(fitGroup);
  scene.add(pivot);

  const floor = new THREE.Mesh(
    new THREE.CircleGeometry(4, 96),
    new THREE.MeshPhysicalMaterial({
      color: 0x0e1012,
      roughness: 0.72,
      metalness: 0.08,
      clearcoat: 0.1,
    }),
  );
  floor.rotation.x = -Math.PI / 2;
  floor.position.y = -0.42;
  floor.receiveShadow = true;
  floor.visible = opts.kind === 'card' || opts.kind === 'duck';
  scene.add(floor);

  function applyTheme() {
    const isLight = document.documentElement.getAttribute('data-theme') === 'light';
    const background = opts.kind === 'maoqiu'
      ? 0x3a3a39
      : (isLight ? 0xffffff : 0x0b0c0e);
    scene.background = new THREE.Color(background);
    if (scene.fog) scene.fog.color.setHex(background);
    floor.material.color.setHex(opts.kind === 'maoqiu' ? 0x3a3a39 : (isLight ? 0xe8ebf1 : 0x0e1012));
    renderer.toneMappingExposure = opts.kind === 'card' && isLight ? 0.68 : 0.8;
    applyCardThemeBalance();
  }

  const composer = new EffectComposer(renderer);
  composer.addPass(new RenderPass(scene, camera));
  const outlinePass = new OutlinePass(new THREE.Vector2(1, 1), scene, camera);
  outlinePass.edgeStrength = 3.2;
  outlinePass.edgeGlow = 0.65;
  outlinePass.edgeThickness = 0.85;
  outlinePass.pulsePeriod = 2.4;
  outlinePass.visibleEdgeColor.set(0xb8ffe9);
  outlinePass.hiddenEdgeColor.set(0x234f74);
  outlinePass.enabled = false;
  composer.addPass(outlinePass);
  const bloomPass = new UnrealBloomPass(
    new THREE.Vector2(1, 1),
    opts.kind === 'card' ? 0.34 : 0.2,
    opts.kind === 'card' ? 0.44 : 0.36,
    opts.kind === 'card' ? 0.98 : 1.12,
  );
  bloomPass.enabled = opts.kind !== 'maoqiu';
  composer.addPass(bloomPass);
  composer.addPass(new OutputPass());

  let model = null;
  let mixer = null;
  let reveal = null;
  let materialStates = [];
  let destroyed = false;
  let running = true;
  let visible = true;
  let animationReady = false;
  let shaderTime = 0;
  const shaderUniforms = [];
  const detachedMaterials = new Set();

  function applyCardThemeBalance() {
    if (opts.kind !== 'card') return;
    const isLight = document.documentElement.getAttribute('data-theme') === 'light';
    bloomPass.strength = isLight ? 0.18 : 0.34;
    bloomPass.radius = isLight ? 0.3 : 0.44;
    materialStates.forEach((state) => {
      if (state.material.name === 'card_holographic_dynamic') {
        state.material.envMapIntensity = isLight ? 1.45 : 2.15;
      }
    });
  }

  function enhanceMaoqiuMaterial(material) {
    const isHair = material.name === 'web_baked_particle_hair_material';
    material.envMapIntensity = isHair ? 1.5 : 1.05;
    if (material.color) material.color.set(isHair ? 0xe4ad65 : 0x9a5424);
    if ('roughness' in material) material.roughness = isHair ? 0.46 : 0.68;
    if ('metalness' in material) material.metalness = 0;
    if ('sheen' in material) material.sheen = isHair ? 0.42 : 0.18;
    if ('sheenColor' in material) material.sheenColor.set(isHair ? 0xf0bd72 : 0x9a5c22);
    if ('sheenRoughness' in material) material.sheenRoughness = isHair ? 0.3 : 0.34;
    if (isHair) {
      // Match the tuned GLB presentation from commits 2579de1/4be47ec:
      // preserve depth testing for layered strand shading while keeping
      // depth writes disabled so overlapping hair remains soft and visible.
      material.depthTest = true;
      material.depthWrite = false;
      material.polygonOffset = true;
      material.polygonOffsetFactor = -1;
      material.polygonOffsetUnits = -1;
      material.side = THREE.DoubleSide;
    }
    material.needsUpdate = true;
  }

  applyTheme();
  const themeObserver = new MutationObserver(applyTheme);
  themeObserver.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ['data-theme'],
  });

  function dynamicDuckSurface(source, geometry) {
    geometry.computeBoundingBox();
    const bounds = geometry.boundingBox;
    const range = bounds.max.clone().sub(bounds.min);
    range.set(
      Math.max(range.x, 0.0001),
      Math.max(range.y, 0.0001),
      Math.max(range.z, 0.0001),
    );

    const material = new THREE.MeshBasicMaterial({
      map: source.map,
      color: 0xffffff,
      side: THREE.DoubleSide,
      toneMapped: true,
    });
    material.name = source.name;
    material.onBeforeCompile = (shader) => {
      shader.uniforms.uDynamicTime = { value: 0 };
      shader.uniforms.uDuckBoundsMin = { value: bounds.min.clone() };
      shader.uniforms.uDuckBoundsRange = { value: range };
      shaderUniforms.push(shader.uniforms);
      shader.vertexShader = shader.vertexShader.replace(
        'void main() {',
        'varying vec3 vDuckPosition;\nvoid main() {',
      );
      shader.vertexShader = shader.vertexShader.replace(
        '#include <begin_vertex>',
        '#include <begin_vertex>\n vDuckPosition = position;',
      );
      shader.fragmentShader = shader.fragmentShader.replace(
        'void main() {',
        `uniform float uDynamicTime;
        uniform vec3 uDuckBoundsMin;
        uniform vec3 uDuckBoundsRange;
        varying vec3 vDuckPosition;

        float duckHash(vec3 p) {
          p = fract(p * .1031);
          p += dot(p, p.yzx + 33.33);
          return fract((p.x + p.y) * p.z);
        }

        float duckNoise(vec3 p) {
          vec3 i = floor(p);
          vec3 f = fract(p);
          f = f * f * (3.0 - 2.0 * f);
          return mix(
            mix(mix(duckHash(i), duckHash(i + vec3(1,0,0)), f.x),
                mix(duckHash(i + vec3(0,1,0)), duckHash(i + vec3(1,1,0)), f.x), f.y),
            mix(mix(duckHash(i + vec3(0,0,1)), duckHash(i + vec3(1,0,1)), f.x),
                mix(duckHash(i + vec3(0,1,1)), duckHash(i + vec3(1,1,1)), f.x), f.y), f.z
          );
        }

        vec3 duckBaseRamp(float t) {
          vec3 c0 = vec3(.12, .20, .62);
          vec3 c1 = vec3(.60, .76, 1.00);
          vec3 c2 = vec3(.96, 1.00, 1.00);
          return t < .45 ? mix(c0, c1, t / .45) : mix(c1, c2, (t - .45) / .55);
        }

        vec3 duckWaveRamp(float t) {
          vec3 c0 = vec3(.04, .10, .32);
          vec3 c1 = vec3(.66, 1.00, .90);
          vec3 c2 = vec3(1.00, 1.00, .96);
          vec3 c3 = vec3(.42, .58, 1.00);
          if (t < .42) return mix(c0, c1, t / .42);
          if (t < .65) return mix(c1, c2, (t - .42) / .23);
          return mix(c2, c3, (t - .65) / .35);
        }
        void main() {`,
      );
      shader.fragmentShader = shader.fragmentShader.replace(
        '#include <map_fragment>',
        `#include <map_fragment>
        vec3 generated = clamp((vDuckPosition - uDuckBoundsMin) / uDuckBoundsRange, 0.0, 1.0);
        vec3 p = generated - .5;
        float mappedAngle = 1.01927;
        mat2 mappedRotation = mat2(cos(mappedAngle), -sin(mappedAngle), sin(mappedAngle), cos(mappedAngle));
        p.xz = mappedRotation * p.xz;
        p.x *= 1.1;

        vec3 drift = vec3(uDynamicTime*.030, -uDynamicTime*.022, uDynamicTime*.018);
        float largeNoise = duckNoise(p*2.0 + drift);
        float fineNoise = duckNoise(p*7.0 - drift*1.7);
        vec3 baseColor = duckBaseRamp(mix(largeNoise, fineNoise, .28));

        float ringRadius = length(p.yz);
        float ringDistortion = (duckNoise(p*4.0 + drift*2.2) - .5) * .82;
        float ringPhase = (ringRadius*4.9 + ringDistortion - uDynamicTime*.095) * 6.2831853;
        float ringFactor = .5 + .5*sin(ringPhase);
        vec3 waveColor = duckWaveRamp(ringFactor);

        float bakedStar = smoothstep(.90, .995, min(min(diffuseColor.r, diffuseColor.g), diffuseColor.b));
        vec3 finalDuck = mix(baseColor, waveColor, .58);
        float deepBlueBand = pow(1.0 - ringFactor, 2.25);
        finalDuck = mix(finalDuck, vec3(.025, .085, .38), deepBlueBand*.78);
        finalDuck = mix(finalDuck, vec3(.88, .96, 1.0), .16);
        diffuseColor.rgb = finalDuck*1.32 + vec3(1.0, 1.0, .88)*bakedStar*1.15;`,
      );
    };
    material.customProgramCacheKey = () => 'duck-source-rings-v1';
    material.needsUpdate = true;
    return material;
  }

  function dynamicCard(material) {
    material.metalness = 0.48;
    material.roughness = 0.16;
    material.clearcoat = 0.86;
    material.clearcoatRoughness = 0.055;
    material.iridescence = 0.92;
    material.iridescenceIOR = 1.45;
    material.iridescenceThicknessRange = [180, 560];
    // The homepage frames the card smaller than the standalone showcase, so
    // compensate for the reduced screen-space highlight without enlarging it.
    material.envMapIntensity = 2.15;
    material.emissive = new THREE.Color(0x111827);
    material.emissiveIntensity = 0.16;
    material.onBeforeCompile = (shader) => {
      shader.uniforms.uDynamicTime = { value: 0 };
      shaderUniforms.push(shader.uniforms);
      shader.fragmentShader = shader.fragmentShader.replace(
        'void main() {',
        `uniform float uDynamicTime;
        vec3 foilSpectrum(float t) {
          return .55 + .45*cos(6.28318*(vec3(.02,.34,.68)+t));
        }
        void main() {`,
      );
      shader.fragmentShader = shader.fragmentShader.replace(
        '#include <map_fragment>',
        `#include <map_fragment>
        float foilBand = pow(.5+.5*sin((vMapUv.x*1.4+vMapUv.y)*18.0-uDynamicTime*1.15), 7.0);
        float foilFine = .5+.5*sin(vMapUv.x*92.0-vMapUv.y*58.0+uDynamicTime*.7);
        vec3 foilColor = foilSpectrum(vMapUv.x*.7-vMapUv.y*.32+uDynamicTime*.045);
        diffuseColor.rgb = mix(diffuseColor.rgb, diffuseColor.rgb*foilColor*1.55, foilBand*.34 + foilFine*.055);`,
      );
      shader.fragmentShader = shader.fragmentShader.replace(
        '#include <emissivemap_fragment>',
        `#include <emissivemap_fragment>
        totalEmissiveRadiance += foilColor*foilBand*.26;`,
      );
    };
    material.customProgramCacheKey = () => 'card-dynamic-v2';
    material.needsUpdate = true;
  }

  function enhanceMaterials(root) {
    outlinePass.selectedObjects = [];
    root.traverse((object) => {
      if (!object.isMesh) return;
      object.castShadow = opts.kind !== 'maoqiu';
      object.receiveShadow = opts.kind !== 'maoqiu';
      const sourceMaterials = Array.isArray(object.material)
        ? object.material
        : [object.material];
      const enhancedMaterials = sourceMaterials.map((material) => {
        if (!material) return material;
        material.envMapIntensity = 1.35;
        if (material.name.startsWith('duck_baked_dynamic__')) {
          detachedMaterials.add(material);
          return dynamicDuckSurface(material, object.geometry);
        }
        if (opts.kind === 'duck' && material.name === 'GUI:非炫彩部位_嘴巴红色材质') {
          material.color.set(0x8c3027);
          material.metalness = 0;
          material.roughness = 0.46;
          material.envMapIntensity = 0.35;
          material.clearcoat = 0.08;
        }
        if (opts.kind === 'duck' && material.name === 'GUI:非炫彩部位_脚部珊瑚色材质') {
          material.color.set(0x5a302d);
          material.metalness = 0;
          material.roughness = 0.58;
          material.envMapIntensity = 0.28;
          material.clearcoat = 0.04;
        }
        if (opts.kind === 'maoqiu') {
          enhanceMaoqiuMaterial(material);
        }
        if (material.name === 'card_holographic_dynamic') dynamicCard(material);
        return material;
      });
      object.material = Array.isArray(object.material)
        ? enhancedMaterials
        : enhancedMaterials[0];
      if (opts.kind === 'duck' && object.name === '身体') {
        outlinePass.selectedObjects = [object];
      }
      if (opts.kind === 'maoqiu' && object.name.includes('hair')) object.renderOrder = 1;
    });
  }

  function setModelOpacity(value) {
    materialStates.forEach((state) => {
      state.material.transparent = true;
      state.material.opacity = state.opacity * value;
      state.material.depthWrite = value > 0.98 ? state.depthWrite : false;
    });
  }

  function restoreMaterials() {
    materialStates.forEach((state) => {
      state.material.transparent = state.transparent;
      state.material.opacity = state.opacity;
      state.material.depthWrite = state.depthWrite;
      state.material.needsUpdate = true;
    });
  }

  function buildRevealPoints(root) {
    if (reduceMotion || opts.reveal === false) {
      restoreMaterials();
      animationReady = true;
      outlinePass.enabled = opts.kind === 'duck';
      return;
    }

    root.updateMatrixWorld(true);
    pivot.updateMatrixWorld(true);
    const inversePivot = new THREE.Matrix4().copy(pivot.matrixWorld).invert();
    const meshes = [];
    let vertexCount = 0;

    root.traverse((object) => {
      const position = object.geometry?.attributes?.position;
      if (!object.isMesh || !position || !object.visible) return;
      meshes.push({ object, position });
      vertexCount += position.count;
    });

    if (!vertexCount) {
      restoreMaterials();
      animationReady = true;
      outlinePass.enabled = opts.kind === 'duck';
      return;
    }

    const maxPoints = isMobile ? 14000 : 32000;
    const stride = Math.max(1, Math.ceil(vertexCount / maxPoints));
    const sampled = [];
    const point = new THREE.Vector3();
    let globalIndex = 0;

    meshes.forEach(({ object, position }) => {
      for (let index = 0; index < position.count; index += 1) {
        const shouldSample = globalIndex % stride === 0;
        globalIndex += 1;
        if (!shouldSample) continue;

        point.fromBufferAttribute(position, index);
        if (object.isSkinnedMesh && typeof object.applyBoneTransform === 'function') {
          object.applyBoneTransform(index, point);
        }
        point.applyMatrix4(object.matrixWorld).applyMatrix4(inversePivot);
        sampled.push(point.x, point.y, point.z);
      }
    });

    const targets = new Float32Array(sampled);
    const starts = new Float32Array(targets.length);
    const positions = new Float32Array(targets.length);

    for (let i = 0; i < targets.length; i += 3) {
      const radius = 0.45 + Math.random() * 1.35;
      const theta = Math.random() * Math.PI * 2;
      const z = Math.random() * 2 - 1;
      const radial = Math.sqrt(1 - z * z);
      starts[i] = targets[i] + Math.cos(theta) * radial * radius;
      starts[i + 1] = targets[i + 1] + Math.sin(theta) * radial * radius;
      starts[i + 2] = targets[i + 2] + z * radius;
    }
    positions.set(starts);

    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    const material = new THREE.PointsMaterial({
      color: ACCENT,
      size: isMobile ? 0.022 : 0.018,
      sizeAttenuation: true,
      transparent: true,
      opacity: 0.95,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    });
    const points = new THREE.Points(geometry, material);
    pivot.add(points);

    setModelOpacity(0);
    reveal = { points, positions, starts, targets, elapsed: 0, duration: 2.2 };
  }

  function finishReveal() {
    if (!reveal) return;
    pivot.remove(reveal.points);
    reveal.points.geometry.dispose();
    reveal.points.material.dispose();
    reveal = null;
    restoreMaterials();
    animationReady = true;
    outlinePass.enabled = opts.kind === 'duck';
  }

  function updateReveal(deltaSeconds) {
    if (!reveal) return;
    reveal.elapsed += deltaSeconds;
    const progress = Math.min(1, reveal.elapsed / reveal.duration);
    const eased = progress * progress * (3 - 2 * progress);

    for (let i = 0; i < reveal.positions.length; i += 1) {
      reveal.positions[i] = reveal.starts[i]
        + (reveal.targets[i] - reveal.starts[i]) * eased;
    }
    reveal.points.geometry.attributes.position.needsUpdate = true;
    reveal.points.material.opacity = 0.95 * (1 - Math.max(0, (progress - 0.62) / 0.38));
    setModelOpacity(Math.max(0, (progress - 0.5) / 0.5));

    if (progress >= 1) finishReveal();
  }

  function prepareModel(gltf) {
    model = gltf.scene;
    fitGroup.add(model);
    enhanceMaterials(model);
    model.updateMatrixWorld(true);

    const box = new THREE.Box3().setFromObject(model);
    const center = box.getCenter(new THREE.Vector3());
    const size = box.getSize(new THREE.Vector3());
    const maxDimension = Math.max(size.x, size.y, size.z) || 1;
    const targetSize = isMobile
      ? (opts.mobileTargetSize ?? 0.9)
      : (opts.targetSize ?? 1.1);
    const scale = targetSize / maxDimension;
    fitGroup.scale.setScalar(scale);
    fitGroup.position.set(
      -center.x * scale,
      -center.y * scale + (opts.offsetY ?? 0.2),
      -center.z * scale,
    );
    fitGroup.updateMatrixWorld(true);

    const materials = new Set();
    model.traverse((object) => {
      if (!object.isMesh) return;
      // The fur asset is a dense baked shell. Avoid frustum-culling its
      // individual mesh while the pivot rotates, which can drop strands when
      // the loader's generated bounds are conservative.
      object.frustumCulled = opts.kind === 'maoqiu' ? false : true;
      const objectMaterials = Array.isArray(object.material)
        ? object.material
        : [object.material];
      objectMaterials.filter(Boolean).forEach((material) => materials.add(material));
    });
    materialStates = Array.from(materials).map((material) => ({
      material,
      transparent: material.transparent,
      opacity: material.opacity,
      depthWrite: material.depthWrite,
    }));
    applyCardThemeBalance();

    if (gltf.animations?.length) {
      mixer = new THREE.AnimationMixer(model);
      gltf.animations.forEach((clip) => mixer.clipAction(clip).play());
    }

    // The baked fur GLB must stay intact. Its hair mesh contains the full
    // strand geometry, so never route it through the generic sampled point
    // reveal even if a caller omits the explicit `reveal: false` option.
    if (opts.kind === 'maoqiu') {
      restoreMaterials();
      animationReady = true;
    } else {
      buildRevealPoints(model);
    }
    if (typeof opts.onReady === 'function') opts.onReady(gltf);
  }

  const loader = new GLTFLoader();
  loader.load(
    opts.url,
    (gltf) => {
      if (destroyed) {
        gltf.scene.traverse((object) => object.geometry?.dispose?.());
        return;
      }
      prepareModel(gltf);
    },
    undefined,
    (error) => {
      if (destroyed) return;
      console.error('[hero-glb] failed to load:', opts.url, error);
      if (typeof opts.onError === 'function') opts.onError(error);
    },
  );

  function resize() {
    const parent = canvas.parentElement || canvas;
    const width = parent.clientWidth || window.innerWidth || 800;
    const height = parent.clientHeight || window.innerHeight || 600;
    renderer.setSize(width, height, false);
    composer.setSize(width, height);
    outlinePass.setSize(width, height);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
  }
  resize();

  let resizeObserver = null;
  if ('ResizeObserver' in window) {
    resizeObserver = new ResizeObserver(resize);
    resizeObserver.observe(canvas.parentElement || canvas);
  } else {
    window.addEventListener('resize', resize);
  }

  let pointerX = 0;
  let pointerY = 0;
  let targetPointerX = 0;
  let targetPointerY = 0;
  let dragRotationX = 0;
  let dragRotationY = 0;
  let dragging = false;
  let dragPointerId = null;
  let dragLastX = 0;
  let dragLastY = 0;

  function onPointerDown(event) {
    if (event.button !== 0 && event.pointerType === 'mouse') return;
    dragging = true;
    dragPointerId = event.pointerId;
    dragLastX = event.clientX;
    dragLastY = event.clientY;
    targetPointerX = 0;
    targetPointerY = 0;
    canvas.classList.add('is-dragging');
    try { canvas.setPointerCapture(event.pointerId); } catch (error) {}
  }

  function onPointerMove(event) {
    if (dragging && event.pointerId === dragPointerId) {
      const deltaX = event.clientX - dragLastX;
      const deltaY = event.clientY - dragLastY;
      dragLastX = event.clientX;
      dragLastY = event.clientY;
      dragRotationY += deltaX * 0.006;
      dragRotationX = THREE.MathUtils.clamp(
        dragRotationX + deltaY * 0.006,
        -1.05,
        1.05,
      );
      return;
    }
    if (isMobile) return;
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    targetPointerX = ((event.clientX - rect.left) / rect.width - 0.5) * 2;
    targetPointerY = ((event.clientY - rect.top) / rect.height - 0.5) * 2;
  }
  function onPointerUp(event) {
    if (!dragging || event.pointerId !== dragPointerId) return;
    dragging = false;
    dragPointerId = null;
    canvas.classList.remove('is-dragging');
    try { canvas.releasePointerCapture(event.pointerId); } catch (error) {}
  }
  function onPointerLeave() {
    if (dragging) return;
    targetPointerX = 0;
    targetPointerY = 0;
  }
  if (interactive) {
    canvas.addEventListener('pointerdown', onPointerDown);
    window.addEventListener('pointermove', onPointerMove, { passive: true });
    window.addEventListener('pointerup', onPointerUp);
    window.addEventListener('pointercancel', onPointerUp);
    window.addEventListener('pointerleave', onPointerLeave);
  }

  let intersectionObserver = null;
  if ('IntersectionObserver' in window) {
    intersectionObserver = new IntersectionObserver(
      ([entry]) => { visible = entry.isIntersecting; },
      { threshold: 0.01 },
    );
    intersectionObserver.observe(canvas);
  }

  function onVisibilityChange() {
    visible = !document.hidden;
  }
  document.addEventListener('visibilitychange', onVisibilityChange);

  let rotation = 0;
  const baseRotationX = opts.kind === 'card' ? 0.06 : 0;
  const baseRotationY = opts.initialRotationY ?? (opts.kind === 'card' ? -0.045 : 0);
  let lastTime = performance.now();
  function frame(now) {
    if (!running) return;
    requestAnimationFrame(frame);
    if (!visible) {
      lastTime = now;
      return;
    }

    const deltaMs = Math.min(50, now - lastTime);
    const deltaSeconds = deltaMs / 1000;
    lastTime = now;

    if (interactive) {
      pointerX += (targetPointerX - pointerX) * 0.055;
      pointerY += (targetPointerY - pointerY) * 0.055;
    } else {
      pointerX = 0;
      pointerY = 0;
    }
    if (!reduceMotion || opts.forceAutoRotate) {
      rotation += (opts.autoRotate ?? 0.00013) * deltaMs;
    }
    pivot.rotation.y = baseRotationY + rotation + dragRotationY + pointerX * 0.28;
    pivot.rotation.x = baseRotationX + dragRotationX + pointerY * 0.16;
    pivot.position.x = pointerX * 0.035;
    pivot.position.y = -pointerY * 0.018;

    updateReveal(deltaSeconds);
    if (mixer && animationReady) mixer.update(deltaSeconds);
    if (animationReady && !reduceMotion) shaderTime += deltaSeconds;
    shaderUniforms.forEach((uniforms) => {
      uniforms.uDynamicTime.value = shaderTime;
    });
    composer.render(deltaSeconds);
  }
  requestAnimationFrame(frame);

  return {
    destroy() {
      running = false;
      destroyed = true;
      resizeObserver?.disconnect();
      intersectionObserver?.disconnect();
      themeObserver.disconnect();
      window.removeEventListener('resize', resize);
      if (interactive) {
        canvas.removeEventListener('pointerdown', onPointerDown);
        window.removeEventListener('pointermove', onPointerMove);
        window.removeEventListener('pointerup', onPointerUp);
        window.removeEventListener('pointercancel', onPointerUp);
        window.removeEventListener('pointerleave', onPointerLeave);
      }
      document.removeEventListener('visibilitychange', onVisibilityChange);

      mixer?.stopAllAction();
      if (reveal) {
        reveal.points.geometry.dispose();
        reveal.points.material.dispose();
      }
      if (model) {
        const disposedMaterials = new Set();
        const disposedTextures = new Set();
        model.traverse((object) => {
          object.geometry?.dispose?.();
          const materials = Array.isArray(object.material)
            ? object.material
            : [object.material];
          materials.filter(Boolean).forEach((material) => {
            if (disposedMaterials.has(material)) return;
            disposedMaterials.add(material);
            Object.values(material).forEach((value) => {
              if (value?.isTexture && !disposedTextures.has(value)) {
                disposedTextures.add(value);
                value.dispose();
              }
            });
            material.dispose();
          });
        });
      }
      environmentTarget.dispose();
      floor.geometry.dispose();
      floor.material.dispose();
      detachedMaterials.forEach((material) => material.dispose());
      outlinePass.dispose();
      bloomPass.dispose();
      composer.dispose();
      renderer.renderLists.dispose();
      renderer.dispose();
    },
  };
}

window.initHeroGLB = initHeroGLB;
