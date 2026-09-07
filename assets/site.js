/* =========================================================
   site.js — micro-interactions for the homepage
   - Scroll-reveal (IntersectionObserver) with section + stagger
   - Magnetic hover on .pub-actions buttons
   - Pointer-driven gradient on .pub-card
   - Smooth-scroll for in-page anchors
   - News collapse toggle
   ========================================================= */
(function () {
  'use strict';

  function onReady(fn) {
    if (document.readyState !== 'loading') fn();
    else document.addEventListener('DOMContentLoaded', fn);
  }

  function revealOnScroll() {
    if (!('IntersectionObserver' in window)) {
      document.querySelectorAll('.reveal, .reveal-stagger, .section')
        .forEach(el => el.classList.add('is-visible'));
      return;
    }
    const io = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) {
          entry.target.classList.add('is-visible');
          io.unobserve(entry.target);
        }
      }
    }, { threshold: 0.12, rootMargin: '0px 0px -40px 0px' });

    document.querySelectorAll('.reveal, .reveal-stagger, .section')
      .forEach(el => io.observe(el));
  }

  function smoothAnchors() {
    document.querySelectorAll('a[href^="#"]').forEach((a) => {
      const href = a.getAttribute('href');
      if (!href || href === '#' || href.length < 2) return;
      a.addEventListener('click', (e) => {
        const target = document.querySelector(href);
        if (!target) return;
        e.preventDefault();
        target.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
    });
  }

  function backToTop() {
    if (document.querySelector('.back-to-top')) return;

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'back-to-top';
    btn.innerHTML = '<i class="fa fa-chevron-up" aria-hidden="true"></i>';
    btn.setAttribute('aria-label', 'Back to top');
    btn.setAttribute('aria-hidden', 'true');
    btn.tabIndex = -1;

    let ticking = false;
    function update() {
      const visible = window.scrollY > 360;
      btn.classList.toggle('is-visible', visible);
      btn.setAttribute('aria-hidden', String(!visible));
      btn.tabIndex = visible ? 0 : -1;
      ticking = false;
    }

    window.addEventListener('scroll', () => {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(update);
    }, { passive: true });

    btn.addEventListener('click', () => {
      const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
      window.scrollTo({ top: 0, behavior: reduced ? 'auto' : 'smooth' });
    });

    document.body.appendChild(btn);
    update();
  }

  function heroExpand() {
    const hero = document.querySelector('.hero');
    const btn = document.querySelector('.hero-expand');
    if (!hero || !btn) return;

    const icon = btn.querySelector('i');
    function render() {
      const expanded = hero.classList.contains('is-expanded');
      btn.setAttribute('aria-pressed', String(expanded));
      btn.setAttribute('aria-label', expanded ? 'Close full-screen 3D viewer' : 'Expand 3D viewer');
      btn.setAttribute('title', expanded ? 'Close full-screen 3D viewer' : 'Expand 3D viewer');
      if (icon) icon.className = expanded ? 'fa fa-times' : 'fa fa-expand';
    }

    function toggle() {
      hero.classList.toggle('is-expanded');
      document.body.classList.toggle('hero-viewer-expanded', hero.classList.contains('is-expanded'));
      render();
    }

    btn.addEventListener('click', toggle);
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && hero.classList.contains('is-expanded')) {
        toggle();
      }
    });
    render();
  }

  function languageToggle() {
    const button = document.getElementById('language-toggle');
    if (!button) return;
    const translations = {
      'Learning 3D coding from real-world 3D creation workflows.': '从真实的 3D 创作流程中学习 3D 编程。',
      'Holographic Foil Card': '镭射闪卡',
      'Iridescent Duck Gigi': '炫彩鸭吉吉',
      'Case 1 · Holographic Card': '案例 1 · 镭射闪卡',
      'Case 2 · Duck Gigi': '案例 2 · 炫彩鸭吉吉',
      'Case 3 · Asuka': '案例 3 · 明日香窗花',
      'Case 4 · Fur Ball': '案例 4 · 毛球运动',
      'Stained Glass Window': '玻璃花窗材质',
      'Stained Glass Asuka': '玻璃花窗材质',
      'Stained-Glass Asuka': '玻璃花窗材质',
      'Chromatic Duck Gigi': '炫彩鸭吉吉',
      'Golden Fur Ball': '金色毛球',
      'Procedural Fur Ball': '毛球运动',
      'Scroll': '下滑',
      'Learning Blender creation from internet tutorial videos.': '从互联网教程视频中学习 Blender 创作。',
      'Abstract': '摘要',
      'Method': '方法',
      'How It Works': '工作原理',
      'What You Get': '你将得到',
      'Generation': '生成',
      'Editing': '编辑',
      'Citation': '引用',
      'Material': '材质',
      'Objects': '物体',
      'Tutorial-learned surfaces, shaders, and animated effects.': '从教程中学习的表面、着色器和动画效果。',
      'Tutorial-built objects and small scenes.': '根据教程构建的物体和小型场景。',
      'Copy': '复制',
      'Dataset': '数据集',
      'The 3D-Coding-Blender authors': '3D-Coding-Blender 作者团队',
      'Final Blender work': '最终 Blender 成果',
      'Source image and tutorial cover to editable Blender material.': '从源图像和教程封面到可编辑的 Blender 材质。'
      , 'The workflow separates the character, background, text, and outline into transparent layers, then offsets them at different depths with view-dependent coordinates to create parallax. Alpha masks preserve each silhouette, while procedural Voronoi and wave patterns add iridescent highlights, sparkles, and a gold outline. Animated reflections complete a holographic foil card that shifts as it moves.': '工作流将角色、背景、文字和轮廓分离为透明图层，再利用随视角变化的坐标将它们放置在不同深度，从而产生视差。Alpha 蒙版保留每个轮廓，程序化 Voronoi 和波纹图案带来虹彩高光、闪烁和金色描边。动态反射完成了会随移动变化的全息箔卡。',
      'Duck Gigi starts from a generated PBR character whose yellow surface is replaced with a procedural iridescent material. Star textures, mapped noise, color ramps, and wave-based bands create the shifting finish; masks preserve the face, feet, and beak. Controlled emission, bloom, and keyframed shader parameters produce the animated color transitions.': '小鸭 Gigi 从生成的 PBR 角色开始，将黄色表面替换为程序化虹彩材质。星形纹理、映射噪声、色带和波纹条带共同形成变化的表面；蒙版保留脸部、脚和喙。受控的发光、辉光和关键帧着色器参数产生动态的颜色过渡。',
      'The agent starts from a compact spherical core and reconstructs a procedural hair system across its surface. Strand density, length, clumping, roughness, and direction are varied to create a soft, naturally uneven silhouette instead of a uniform shell. Warm color variation and strand-aware shading preserve depth between the fibers, while the hidden core prevents gaps as the asset rotates. The result is an editable golden fur material whose density, length, shape, and palette can be adjusted through the same procedural setup.': '智能体从紧凑的球形核心开始，在其表面重建程序化毛发系统。通过调整毛发密度、长度、聚束、粗糙度和方向，形成柔软且自然不均匀的轮廓，而不是均匀外壳。暖色变化和感知毛发的着色保留纤维之间的层次，隐藏的核心则避免模型旋转时出现空隙。最终得到可编辑的金色毛发材质，其密度、长度、形状和色盘都可通过同一套程序化设置调整。',
      'The agent first recovers the geometric setup—a thin, solidified plane that gives the material physical thickness—then rebuilds the shader graph around the source artwork. A mapped image texture provides the local colors, while a 2D Voronoi texture in Distance to Edge mode partitions the picture into irregular glass panes. ColorRamp nodes sharpen these boundaries into a reusable mask, which separates the translucent colored glass from its dark metallic leading and also drives raised and inset bump details along the seams. Additional noise-based distortion breaks up overly regular borders, introduces subtle surface waviness, and enriches the color variation inside each pane. The resulting asset adapts this procedural workflow to the Asuka artwork, preserving the original composition while giving it the depth, transmission, reflections, and handcrafted mosaic character of a luminous stained-glass window.': '智能体首先恢复几何结构：一个经过实体化、赋予材质物理厚度的薄平面；随后围绕源图重建着色器节点图。映射图像纹理提供局部颜色，二维 Voronoi 纹理的 Distance to Edge 模式将画面划分为不规则玻璃面板。ColorRamp 节点将边界锐化为可复用蒙版，分离半透明彩色玻璃与深色金属铅条，同时驱动接缝处凸起和凹陷的凹凸细节。额外的噪声失真打破过于规则的边界，引入细微表面起伏并丰富每块玻璃内部的颜色变化。最终资产将这套程序化流程应用于 Asuka 艺术图，保留原始构图，同时呈现发光彩色玻璃窗的深度、透射、反射和手工马赛克质感。',
      'Learning Blender creation from internet tutorial videos.': '从互联网教程视频中学习 Blender 创作。',
      'Internet Blender tutorials contain rich, real-world creation knowledge, but that knowledge is difficult for an agent to use directly. Important instructions may appear in narration, on-screen captions, changing interface states, or brief node-graph operations, while the final result alone does not reveal how the asset was constructed. We present 3D-Coding-Blender, an agent pipeline that converts tutorial videos into timestamped multimodal evidence, reconstructs the demonstrated workflow, and generates executable Blender Python from a canonical scene.': '互联网 Blender 教程包含丰富的真实创作知识，但智能体难以直接使用这些知识。重要指令可能出现在旁白、屏幕字幕、变化的界面状态或简短的节点图操作中，而最终结果本身无法说明资产是如何构建的。我们提出 3D-Coding-Blender：一个将教程视频转换为带时间戳的多模态证据、重建示范工作流，并从规范场景生成可执行 Blender Python 的智能体流程。',
      'The agent combines visual keyframes, OCR, available transcript evidence, Blender-version cues, and retrieved workflow knowledge to build a bounded reconstruction specification. Generated programs are executed inside Blender and evaluated through fresh renders, multi-view observations, and turntable or animation evidence when motion is supported by the tutorial. The primary output is an editable': '智能体结合视觉关键帧、OCR、可用的字幕证据、Blender 版本线索和检索到的工作流知识，构建边界明确的重建规范。生成的程序在 Blender 中执行，并通过新渲染、多视角观察，以及教程支持动作时的转台或动画证据进行评估。主要输出是可编辑的',
      'asset rather than a flattened image.': '资产，而不只是图像。',
      'Successful reconstructions can be retained as candidate procedural knowledge for future tasks. A complementary editing workflow applies learned geometry and material techniques to existing assets while preserving non-target scene content and validating the result through before-and-after visual review.': '成功的重建结果可以作为候选程序化知识保留，用于未来任务。配套的编辑工作流将学习到的几何和材质技术应用于现有资产，同时保留非目标场景内容，并通过编辑前后的视觉检查验证结果。',
      'Five-stage 3D-Coding-Blender pipeline from internet tutorials through multimodal evidence, workflow specification, coding-agent execution, and verified deliverables': '从互联网教程、多模态证据、工作流规范、代码智能体执行到验证交付物的五阶段 3D-Coding-Blender 流程',
      '3D-Coding-Blender pipeline overview': '3D-Coding-Blender 流程概览',
      'Start from high-quality internet Blender tutorials and identify the target asset, workflow, and supported motion.': '从高质量互联网 Blender 教程开始，确定目标资产、工作流和支持的动作。',
      'Align keyframes, OCR, narration or audio, interface actions, timestamps, and Blender-version cues.': '对齐关键帧、OCR、旁白或音频、界面操作、时间戳和 Blender 版本线索。',
      'Convert evidence into ordered operations and retrieve relevant modeling, material, lighting, and animation knowledge.': '将证据转换为有序操作，并检索相关的建模、材质、灯光和动画知识。',
      'Generate Blender Python, execute it in Blender, render the scene, compare the result, and repair failures in a closed loop.': '生成 Blender Python，在 Blender 中执行并渲染场景，比较结果并在闭环中修复失败。',
      'Package editable assets and visual evidence, then retain validated workflow patterns as candidate procedural knowledge for reuse.': '打包可编辑资产与视觉证据，并将经过验证的工作流模式保留为候选程序化知识，以供后续复用。',
      'An End-to-End Agent Pipeline': '端到端智能体流程', 'Each run recreates a tutorial workflow and delivers an editable Blender project, a reproduction script, renders, and validation results.': '每次运行都会重建教程工作流，并交付可编辑的 Blender 工程、复现脚本、渲染结果和验证结果。',
      'A High-Quality 3D Dataset': '高质量 3D 数据集', 'Through collection, repair, and reconstruction, we built a high-quality dataset of 23K procedural 3D assets.': '通过收集、修复与复现，我们构建了一个包含 23K 个程序化 3D 资产的高质量数据集。',
      'A Reusable Knowledge Library': '可复用知识库', 'Retrieval-ready modeling, material, lighting, animation, and workflow patterns distilled from successful runs.': '从成功运行中提炼、可供检索的建模、材质、灯光、动画和工作流模式。',
      'Tutorials become executable, editable assets.': '教程变成可执行、可编辑的资产。', 'Generation workflow': '生成工作流', 'Editing workflow': '编辑工作流',
      'Original yellow asset and material tutorial to a generated chromatic result.': '从原始黄色资产和材质教程到生成的炫彩结果。',
      'Input': '输入', 'Pipeline': '流程', 'Executable outputs': '可执行输出', 'Blender material nodes': 'Blender 材质节点', 'Blender Python': 'Blender Python', 'Render': '渲染',
      'Editable forms, scientific visualizations, and compact scene studies.': '可编辑的形体、科学可视化和紧凑的场景研究。',
      'Learned techniques reshape existing assets while preserving what should stay.': '学习到的技术重塑现有资产，同时保留应当保留的内容。',
      'One material language, transferred across forms.': '将同一种材质语言迁移到不同形体。',
      'Collect Tutorials': '收集教程', 'Recover Evidence': '恢复证据', 'Specify the Workflow': '定义工作流', 'Code, Run, Repair': '编码、运行、修复', 'Verify and Retain': '验证与保留',
      'Editable Asset': '可编辑资产', 'Reproduction Script': '复现脚本', 'Reconstruction deliverables': '复现交付物',
      'Materials & Node Graphs': '材质与节点图', 'Final Render': '渲染结果', 'Multi-View Renders': '多视角效果图', 'Animation': '效果动画', 'Agent Log': 'Agent 轨迹'
      , 'Learning 3D Coding from Internet Tutorial Videos.': '从互联网教程视频中学习程序化 3D 生成'
      , 'Learning 3D Coding from Internet Tutorial Videos': '从互联网教程视频中学习程序化 3D 生成'
      , 'Generalization': '泛化能力'
      , 'Generalization is central to BlenderLore. Successful reconstructions are retained as candidate procedural knowledge. When facing an unfamiliar generation or editing task, the agent decomposes the target into reusable construction patterns, retrieves relevant procedural knowledge and recombines it into task-specific Blender code.': '泛化能力是 BlenderLore 的核心。成功的复现结果会被保留为候选程序化知识。面对未见过的生成或编辑任务时，智能体先将目标分解为可复用的构建模式，检索相关程序化知识，并将其重组为面向具体任务的 Blender 代码。'
      , 'BlenderLore pipeline overview': 'BlenderLore 流程概览'
      , 'Five-stage BlenderLore pipeline from internet tutorials through multimodal evidence, workflow specification, coding-agent execution, and verified deliverables': '从互联网教程、多模态证据、工作流规范、代码智能体执行到验证交付物的五阶段 BlenderLore 流程'
      , 'A Reusable Procedural Knowledge Library': '可复用的程序化知识库'
      , 'The procedural knowledge library serves as a key mechanism for generalization: when facing an unfamiliar target, the agent decomposes it into reusable construction patterns, retrieves relevant procedural knowledge, and recombines it into task-specific Blender code.': '程序化知识库是实现泛化的关键机制：面对从未见过的任务时，智能体将其分解为可复用的构建模式，检索相关程序化知识，并将其重新组合为最终的 Blender 代码。'
      , 'Generation': '生成', 'Editing': '编辑', 'Tutorials become executable, editable assets.': '教程变成可执行、可编辑的资产。'
      , 'Stained Glass Asuka': '明日香玻璃花窗材质', 'Source image and tutorial cover to editable Blender material.': '从源图像和教程封面到可编辑的 Blender 材质。'
      , 'Input': '输入', 'Executable outputs': '可执行输出', 'Render': '渲染', 'Final Blender work': '最终 Blender 成果'
      , 'Duck Gigi: Iridescent Skins': '鸭吉吉：替换炫彩皮肤', 'Sofa Material Swaps': '沙发材质替换', 'Glass Heart Material Transfer': '玻璃心材质迁移到不同物体'
      , 'One material language, transferred across forms.': '将同一种材质语言迁移到不同形体。'
      , 'Tutorial-learned surfaces, shaders, and animated effects.': '从教程中学习的表面、着色器和动画效果。'
      , 'Editable forms, scientific visualizations, and compact scene studies.': '可编辑的形体、科学可视化和紧凑的场景研究。'
      , 'An End-to-End Agent Pipeline': '端到端智能体流程', 'A High-Quality 3D Dataset': '高质量 3D 数据集'
      , 'Each run recreates a tutorial workflow and delivers an editable Blender project, a reproduction script, renders, and validation results.': '每次运行都会重建教程工作流，并交付可编辑的 Blender 工程、复现脚本、渲染结果和验证结果。'
      , 'Through collection, repair, and reconstruction, we built a high-quality dataset of 23K procedural 3D assets.': '通过收集、修复与复现，我们构建了一个包含 23K 个程序化 3D 资产的高质量数据集。'
      , 'How It Works': '工作原理', 'Collect Tutorials': '收集教程', 'Recover Evidence': '恢复证据', 'Specify the Workflow': '定义工作流', 'Code, Run, Repair': '编码、运行、修复', 'Verify and Retain': '验证与保留'
      , 'Start from high-quality internet Blender tutorials and identify the target asset, workflow, and supported motion.': '从高质量互联网 Blender 教程开始，确定目标资产、工作流和支持的动作。'
      , 'Align keyframes, OCR, narration or audio, interface actions, timestamps, and Blender-version cues.': '对齐关键帧、OCR、旁白或音频、界面操作、时间戳和 Blender 版本线索。'
      , 'Convert evidence into ordered operations and retrieve relevant procedural knowledge.': '将证据转换为有序操作，并检索相关程序化知识。'
      , 'Generate Blender Python, execute it in Blender, render the scene, compare the result, and repair failures in a closed loop.': '生成 Blender Python，在 Blender 中执行并渲染场景，比较结果并在闭环中修复失败。'
      , 'Package editable assets and visual evidence, then retain validated workflow patterns as candidate procedural knowledge for reuse.': '打包可编辑资产与视觉证据，并将经过验证的工作流模式保留为候选程序化知识，以供后续复用。'
      , 'Internet Blender tutorials contain rich, real-world creation knowledge, but that knowledge is difficult for an agent to use directly. Important instructions may appear in narration, on-screen captions, changing interface states, or brief node-graph operations, while the final result alone does not reveal how the asset was constructed. We present BlenderLore, an agent pipeline that converts tutorial videos into timestamped multimodal evidence, reconstructs the demonstrated workflow, and generates executable Blender Python from a canonical scene.': '互联网 Blender 教程包含丰富的真实创作知识，但智能体难以直接使用这些知识。重要指令可能出现在旁白、屏幕字幕、变化的界面状态或简短的节点图操作中，而最终结果本身无法说明资产是如何构建的。我们提出 BlenderLore：一个将教程视频转换为带时间戳的多模态证据、重建示范工作流，并从规范场景生成可执行 Blender Python 的智能体流程。'
      , 'Gold Foil Card': '烫金闪卡', 'Laser Card': '镭射卡片', 'Fuse-Bead Material': '拼豆材质'
      , 'Cat Paw Jelly': '猫爪果冻', 'Bee Keycap': '蜜蜂键帽', 'Shaved Ice': '刨冰', 'Bear Ice Zongzi': '冰粽熊', 'Pearl Necklace': '珍珠项链', 'Little Ghost': '小幽灵', 'Toon-Style Render': '三渲二', 'Toon-Style Room': '卡通风格房间', 'Low-Poly House': '低多边形房屋'
      , 'Original Glass Heart': '原始玻璃心', 'Frog Traveler': '青蛙旅者', 'Horned Feathered Creature': '角羽生物', 'Oni Samurai': '鬼武士', 'Female Bust': '女性胸像', '2B Sculpture': '2B 雕塑', 'African Wild Dog': '非洲野犬', 'Bentwood Cane Chair': '弯木藤椅', 'Vintage Racing Car': '复古赛车', 'Classical Sailing Ship': '古典帆船'
      , 'The agent separates the character, background, text, and outline into transparent layers, then offsets them at different depths with view-dependent coordinates to create parallax. Alpha masks preserve each silhouette, while procedural Voronoi and wave patterns add iridescent highlights, sparkles, and a gold outline. Animated reflections complete a holographic foil card that shifts as it moves.': '智能体将角色、背景、文字和轮廓分离为透明图层，再利用随视角变化的坐标将它们放置在不同深度，从而产生视差。Alpha 蒙版保留每个轮廓，程序化 Voronoi 和波纹图案带来虹彩高光、闪烁和金色描边。动态反射完成了会随移动变化的全息箔卡。'
    };
    const replacements = [
      ['Internet Blender tutorials', '互联网 Blender 教程'],
      ['tutorial videos', '教程视频'],
      ['real-world 3D creation workflows', '真实的 3D 创作流程'],
      ['The agent', '智能体'], ['workflows', '工作流'], ['workflow', '工作流'],
      ['Generated material assets', '生成的材质资产'], ['Generated objects and scenes', '生成的物体和场景'],
      ['Material · Motion', '材质 · 动画'], ['Stylized Material · Motion', '风格化材质 · 动画'],
      ['Object Modeling · Motion', '物体建模 · 动画'], ['Object Modeling · Scene', '物体建模 · 场景'],
      ['Biomedical Visualization', '生物医学可视化'], ['Appearance Edit', '外观编辑'],
      ['Original Material · Motion', '原始材质 · 动画'], ['Glass Material · Motion', '玻璃材质 · 动画'],
      ['Material Edit', '材质编辑'], ['Original', '原始'], ['Tutorial', '教程'],
      ['Motion', '动画'], ['Source image', '源图像'], ['Final Blender work', '最终 Blender 成果'],
      ['The primary output is an editable', '主要输出是可编辑的'],
      ['rather than a flattened image', '而不是扁平化图像'],
      ['The result is an editable', '最终结果是可编辑的'],
      ['This project', '本项目'], ['Click', '点击'], ['Open', '打开'], ['Close', '关闭'],
      ['Case ', '案例 '], ['Editable', '可编辑'], ['Reproducible', '可复现'], ['The BlenderLore authors', 'BlenderLore 作者团队'],
      ['Golden Fur Ball', '金色毛球'], ['Black Wukong', '黑悟空'], ['Opening Door', '开门猫'], ['Armor Hero', '装甲英雄'], ['Maodie', '猫蝶'],
      ['Stained Glass Asuka', '彩色玻璃 Asuka'], ['Chromatic Duck Gigi', '炫彩鸭 Gigi'], ['Holographic Foil Card', '全息箔卡'], ['Iridescent Duck Gigi', '彩虹变色鸭 Gigi'], ['Stained Glass Window', '彩色玻璃窗'],
      ['Cat Paw Jelly', '猫爪果冻'], ['Bee Keycap', '蜜蜂键帽'], ['Shaved Ice', '刨冰'], ['Bear Ice Zongzi', '熊猫冰粽'], ['Pearl Necklace', '珍珠项链'], ['Candle', '蜡烛'], ['Little Ghost', '小幽灵'], ['Bow', '蝴蝶结'], ['Tulip', '郁金香'], ['Peach', '桃子'], ['Toon-Style Render', '卡通风格渲染'], ['Toon-Style Room', '卡通风格房间'], ['Material Switch', '材质切换'], ['Neuron', '神经元'], ['Cell Membrane', '细胞膜'], ['Cell', '细胞'],
      ['Duck Gigi: Iridescent Skins', 'Duck Gigi：虹彩外观'], ['Chromatic Study I', '炫彩研究 I'], ['Chromatic Study II', '炫彩研究 II'], ['Chromatic Study III', '炫彩研究 III'], ['Chromatic Study IV', '炫彩研究 IV'], ['Chromatic Study V', '炫彩研究 V'],
      ['Sofa Material Swaps', '沙发材质替换'], ['Bread Sofa', '面包沙发'], ['Forest Green Velvet', '森林绿天鹅绒'], ['Thick-Glazed Enamel', '厚釉珐琅'], ['Glacier Blue', '冰川蓝'], ['Hammered Bronze', '锤纹青铜'], ['Polar Night Teal', '极夜蓝绿'], ['Deep-Space Blue', '深空蓝'], ['Ruby Galaxy', '红宝石星系'], ['Graphite Iridescence', '石墨虹彩'], ['Molten Spectrum', '熔融光谱'], ['Black Pearl', '黑珍珠'], ['Cobalt Crackle', '钴蓝裂纹'], ['Burgundy Velvet', '勃艮第天鹅绒'], ['Celadon Crackle', '青瓷裂纹'], ['Museum Terrazzo', '博物馆水磨石'], ['Citrus Bubble Resin', '柑橘气泡树脂'], ['Green Apple Resin', '青苹果树脂'], ['Ink & Copper Brocade', '墨色铜金织锦'],
      ['Glass Heart Material Transfer', '玻璃心材质迁移'], ['Original Glass Heart', '原始玻璃心'], ['Frog Traveler', '青蛙旅者'], ['Horned Feathered Creature', '角羽生物'], ['Oni Samurai', '鬼武士'], ['Female Bust', '女性胸像'], ['2B Sculpture', '2B 雕塑'], ['African Wild Dog', '非洲野犬'], ['Bentwood Cane Chair', '弯木藤椅'], ['Vintage Racing Car', '复古赛车'], ['Classical Sailing Ship', '古典帆船'],
      ['Tutorial cover', '教程封面'], ['Source image', '源图像'], ['Final Blender work', '最终 Blender 成果'], ['Appearance Edit', '外观编辑'], ['Material Edit', '材质编辑'], ['Stellar Eclipse', '恒星日蚀'], ['Prismatic Holographic', '棱彩全息'], ['Opal Caustics', '蛋白石焦散'], ['Aurora Chrome', '极光铬'], ['Neon Kintsugi', '霓虹金缮'], ['Directional Velvet', '方向性天鹅绒'], ['Glazed Ceramic', '釉面陶瓷'], ['Mineral Terrazzo', '矿物水磨石'], ['Bubble Resin', '气泡树脂'], ['Woven Brocade', '织锦'],
      ['on Bilibili', '（Bilibili）'], ['360-degree turntable', '360 度转台展示'], ['Interactive ', '交互式'], [' animation', ' 动画'], [' visualization', ' 可视化'], [' material', ' 材质'], [' model', '模型'], [' scene', '场景'], ['Original yellow asset', '原始黄色资产'], ['chromatic material tutorial', '炫彩材质教程'], ['compact Blender material node graph', '紧凑的 Blender 材质节点图'], ['Open the ', '打开'], [' tutorial', ' 教程'], [' on ', '，位于'], ['Bilibili ', 'Bilibili '],
      ['stained glass material tutorial cover', '彩色玻璃材质教程封面'], ['chromatic material tutorial cover', '炫彩材质教程封面'], ['Original geometric Asuka artwork used to generate the stained glass material', '用于生成彩色玻璃材质的原始 Asuka 几何艺术图'], ['turntable', '转台展示'], ['cover', '封面'], ['render', '渲染'],
      ['Open ', '打开'], [' enlarged', ' 放大预览'], ['Expand 3D viewer', '展开 3D 查看器'], ['Close full-screen 3D viewer', '关闭全屏 3D 查看器'], ['Loading animation', '正在加载动画'], ['Selected 3D animation', '已选择的 3D 动画'], ['Project resources', '项目资源'], ['Page sections', '页面章节'], ['Copy citation to clipboard', '复制引用到剪贴板'], ['Open enlarged card', '打开大图卡片'], ['Open the original', '打开原始'], ['Open the', '打开'], [' on Bilibili', '（Bilibili）'],
      ['original', '原始'], ['Final', '最终'], ['assets', '资产'], ['asset', '资产'], ['forms', '形体'], ['scientific', '科学'], ['high-quality', '高质量'], ['editable', '可编辑'], ['reproducible', '可复现'], ['validated', '已验证'], ['supported', '支持的'], ['execution', '执行'], ['receipt', '记录'], ['trace', '追踪'],
      ['stained glass', '彩色玻璃'], ['chromatic', '炫彩'], ['Glass Heart', '玻璃心'], ['tutorial', '教程'], ['material', '材质'], ['cover', '封面'], ['render', '渲染'],
      ['contain rich, real-world creation knowledge', '包含丰富的真实创作知识'], ['but that knowledge is difficult for an agent to use directly', '但智能体难以直接使用这些知识'], ['Important instructions may appear in narration', '重要指令可能出现在旁白'], ['on-screen captions', '屏幕字幕'], ['changing interface states', '变化的界面状态'], ['or brief node-graph operations', '或简短的节点图操作'], ['while the final result alone does not reveal how the asset was constructed', '而最终结果本身无法说明资产是如何构建的'], ['an agent pipeline that converts', '一个将'], ['into timestamped multimodal evidence', '转换为带时间戳的多模态证据'], ['reconstructs the demonstrated workflow', '重建示范工作流'], ['and generates executable Blender Python from a canonical scene', '并从规范场景生成可执行 Blender Python'],
      ['The agent combines visual keyframes', '智能体结合视觉关键帧'], ['available transcript evidence', '可用的字幕证据'], ['Blender-version cues', 'Blender 版本线索'], ['retrieved workflow knowledge', '检索到的工作流知识'], ['to build a bounded reconstruction specification', '构建边界明确的重建规范'], ['Generated programs are executed inside Blender', '生成的程序在 Blender 中执行'], ['and evaluated through fresh renders', '并通过新渲染进行评估'], ['multi-view observations', '多视角观察'], ['and turntable or animation evidence', '以及转台或动画证据'], ['when motion is supported by the tutorial', '在教程支持动作时'], ['The primary output is an editable', '主要输出是可编辑的'], ['asset rather than a flattened image', '资产，而不是扁平化图像'],
      ['is central to BlenderLore', '是 BlenderLore 的核心'], ['Successful reconstructions are retained as candidate procedural knowledge', '成功的复现结果会被保留为候选程序化知识'], ['When facing an unfamiliar generation or editing task', '面对未见过的生成或编辑任务时'], ['the agent decomposes the target into reusable construction patterns', '智能体先将目标分解为可复用的构建模式'], ['retrieves relevant procedural knowledge', '检索相关程序化知识'], ['and recombines it into task-specific Blender code', '并将其重组为面向具体任务的 Blender 代码'],
      ['Learned techniques reshape existing assets while preserving what should stay', '学习到的技术重塑现有资产，同时保留应当保留的内容'], ['Original yellow asset and material tutorial to a generated chromatic result', '从原始黄色资产和材质教程到生成的炫彩结果'], ['Make an Iridescent Material While Catching a Pokémon!', '捕捉宝可梦时制作虹彩材质！'],
      ['We present', '我们提出'], ['while the final result alone', '而最终结果本身'],
      ['Holographic Card', '镭射卡片'], ['Duck Gigi', '小鸭 Gigi'], ['Fur Ball', '毛球'], ['Objects', '物体'], ['Citation', '引用'], ['Copy', '复制'],
      ['Material · Motion', '材质 · 动画'], ['Stylized Material · Motion', '风格化材质 · 动画'], ['Object Modeling · Motion', '物体建模 · 动画'], ['Object Modeling · Scene', '物体建模 · 场景'], ['Biomedical Visualization', '生物医学可视化'], ['Original Material · Motion', '原始材质 · 动画'], ['Glass Material · Motion', '玻璃材质 · 动画'],
      ['Forest Green Velvet', '森林绿天鹅绒'], ['Thick-Glazed Enamel', '厚釉珐琅'], ['Glacier Blue', '冰川蓝'], ['Hammered Bronze', '锤纹青铜'], ['Polar Night Teal', '极夜蓝绿'], ['Deep-Space Blue', '深空蓝'], ['Ruby Galaxy', '红宝石星系'], ['Graphite Iridescence', '石墨虹彩'], ['Molten Spectrum', '熔融光谱'], ['Black Pearl', '黑珍珠'], ['Cobalt Crackle', '钴蓝裂纹'], ['Burgundy Velvet', '勃艮第天鹅绒'], ['Celadon Crackle', '青瓷裂纹'], ['Museum Terrazzo', '博物馆水磨石'], ['Citrus Bubble Resin', '柑橘气泡树脂'], ['Green Apple Resin', '青苹果树脂'], ['Ink & Copper Brocade', '墨色铜金织锦'],
      ['This project', '本项目'], ['Click', '点击'], ['Open', '打开'], ['Close', '关闭']
    ];
    function translate(value) {
      const trimmed = value.trim();
      if (!trimmed) return value;
      if (translations[trimmed]) return value.replace(trimmed, translations[trimmed]);
      let result = value;
      replacements
        .slice()
        .sort((a, b) => b[0].length - a[0].length)
        .forEach(([from, to]) => { result = result.split(from).join(to); });
      return result;
    }
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) {
      const node = walker.currentNode;
      const parent = node.parentElement;
      if (!parent || parent.closest('script,style,code,#language-toggle')) continue;
      if (node.nodeValue.trim()) nodes.push(node);
    }
    nodes.forEach((node) => { node.__langEn = node.nodeValue; node.__langZh = translate(node.nodeValue); });
    const attributes = [];
    document.querySelectorAll('[aria-label],[title],[alt]').forEach((element) => {
      ['aria-label', 'title', 'alt'].forEach((name) => {
        if (!element.hasAttribute(name)) return;
        const value = element.getAttribute(name);
        attributes.push({ element, name, en: value, zh: translate(value) });
      });
    });
    let language = 'en';
    try { language = localStorage.getItem('site-language') || 'en'; } catch (e) {}
    function render() {
      nodes.forEach((node) => { node.nodeValue = language === 'zh' ? node.__langZh : node.__langEn; });
      attributes.forEach(({ element, name, en, zh }) => element.setAttribute(name, language === 'zh' ? zh : en));
      button.textContent = language === 'zh' ? 'English' : '中文';
      button.setAttribute('aria-label', language === 'zh' ? 'Switch to English' : '切换到中文');
      document.documentElement.lang = language === 'zh' ? 'zh-CN' : 'en';
      document.title = language === 'zh'
        ? 'BlenderLore：从互联网教程视频中学习程序化 3D 生成'
        : 'BlenderLore: Learning 3D Coding from Internet Tutorial Videos';
    }
    const observer = new MutationObserver((records) => {
      if (language !== 'zh') return;
      records.forEach(({ addedNodes }) => {
        addedNodes.forEach((root) => {
          if (root.nodeType !== Node.ELEMENT_NODE && root.nodeType !== Node.TEXT_NODE) return;
          const fragment = root.nodeType === Node.TEXT_NODE ? root.parentElement : root;
          if (!fragment || fragment.closest('script,style,code,#language-toggle')) return;
          const scan = document.createTreeWalker(fragment, NodeFilter.SHOW_TEXT);
          while (scan.nextNode()) {
            const node = scan.currentNode;
            if (!node.nodeValue.trim() || node.parentElement?.closest('script,style,code,#language-toggle')) continue;
            node.__langEn ||= node.nodeValue;
            node.__langZh ||= translate(node.nodeValue);
            node.nodeValue = node.__langZh;
          }
        });
      });
    });
    observer.observe(document.body, { childList: true, subtree: true });
    button.addEventListener('click', () => {
      language = language === 'zh' ? 'en' : 'zh';
      try { localStorage.setItem('site-language', language); } catch (e) {}
      render();
    });
    render();
  }

  // Hero / lite-mode state -----------------------------------------------
  let heroScene = null;
  let heroPendingInit = false;

  function tryStartHero() {
    if (heroScene || heroPendingInit) return;
    if (typeof window.initHeroMesh !== 'function') {
      // hero-mesh.js (ES module) hasn't executed yet; retry shortly
      heroPendingInit = true;
      const retry = () => {
        heroPendingInit = false;
        if (!document.body.classList.contains('is-lite')) initHero();
      };
      setTimeout(retry, 60);
      return;
    }
    const canvas = document.getElementById('hero-canvas');
    if (!canvas) return;
    heroScene = window.initHeroMesh(canvas, {
      autoRotate: 0.00045,
    });
  }

  function initHero() { tryStartHero(); }

  function destroyHero() {
    if (heroScene && typeof heroScene.destroy === 'function') {
      heroScene.destroy();
    }
    heroScene = null;
  }

  function liteToggle() {
    const STORE_KEY = 'lite-mode';
    const hero = document.querySelector('.hero');
    const lite = document.querySelector('.site-header');
    if (!hero || !lite) return;

    const btn = document.createElement('button');
    btn.className = 'lite-toggle';
    btn.type = 'button';

    function render() {
      const isLite = document.body.classList.contains('is-lite');
      btn.innerHTML = isLite
        ? '<i class="fa fa-magic"></i><span>Restore animation</span>'
        : '<i class="fa fa-bolt"></i><span>Lite mode</span>';
      btn.setAttribute(
        'aria-label',
        isLite ? 'Restore the animated hero' : 'Switch to a lighter version of this page'
      );
      btn.setAttribute('title', btn.getAttribute('aria-label'));
    }

    // Apply persisted choice without animating on initial load
    const saved = (() => {
      try { return localStorage.getItem(STORE_KEY); } catch (e) { return null; }
    })();
    if (saved === '1') {
      document.body.classList.add('is-lite');
    } else {
      // Default: full hero — kick off particles
      initHero();
    }
    render();

    let busy = false;
    function setMode(toLite) {
      if (busy) return;
      const cur = document.body.classList.contains('is-lite');
      if (cur === toLite) return;
      busy = true;

      const fadingOut = toLite ? hero : lite;
      const fadingIn  = toLite ? lite : hero;

      // Phase 1: fade out the current view
      fadingOut.style.opacity = '0';
      fadingOut.style.pointerEvents = 'none';

      setTimeout(() => {
        // Phase 2: swap visibility via body class
        document.body.classList.toggle('is-lite', toLite);

        // Reset inline styles before fade-in (CSS controls final state)
        fadingOut.style.opacity = '';
        fadingOut.style.pointerEvents = '';

        // Force browser to paint hidden state, then fade in
        fadingIn.style.opacity = '0';
        // eslint-disable-next-line no-unused-expressions
        fadingIn.offsetHeight;
        requestAnimationFrame(() => {
          fadingIn.style.opacity = '1';
        });
        setTimeout(() => { fadingIn.style.opacity = ''; }, 450);

        // Particles lifecycle
        if (toLite) destroyHero();
        else initHero();

        try { localStorage.setItem(STORE_KEY, toLite ? '1' : '0'); } catch (e) {}
        render();
        busy = false;
      }, 380);
    }

    btn.addEventListener('click', () => {
      setMode(!document.body.classList.contains('is-lite'));
    });

    document.body.appendChild(btn);
  }

  onReady(() => {
    if (document.body.classList.contains('surflo-page')) {
      document.documentElement.setAttribute('data-theme', 'dark');
    }
    // Remove any stale theme-toggle node left by a previously cached script.
    document.querySelectorAll('.theme-toggle').forEach((el) => el.remove());
    liteToggle();
    revealOnScroll();
    smoothAnchors();
    backToTop();
    heroExpand();
    languageToggle();
  });
})();
