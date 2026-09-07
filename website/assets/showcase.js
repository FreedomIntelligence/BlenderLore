(function () {
  'use strict';

  const legacySelectors = [
    '#meta',
    '.section-video',
    '.section-tldr-cards',
    '.section-tldr',
    '.section-problem',
    '.section-scaling',
    '.section-resolution',
    '.section-guidance',
    '.section-comparison',
    '.section-quantitative',
    '.section-dataset'
  ];

  document.querySelectorAll(legacySelectors.join(',')).forEach((element) => element.remove());

  document.querySelectorAll('.showcase-tabs').forEach((tabList) => {
    const tabs = Array.from(tabList.querySelectorAll('[data-showcase-tab]'));

    function activateTab(activeTab) {
      tabs.forEach((tab) => {
        const selected = tab === activeTab;
        const panel = document.getElementById(tab.dataset.showcaseTab);
        tab.classList.toggle('is-active', selected);
        tab.setAttribute('aria-selected', String(selected));
        tab.tabIndex = selected ? 0 : -1;
        if (panel) {
          panel.hidden = !selected;
          panel.classList.toggle('is-active', selected);
          if (!selected) panel.querySelectorAll('video').forEach((video) => video.pause());
        }
      });
    }

    tabs.forEach((tab, index) => {
      tab.addEventListener('click', () => activateTab(tab));
      tab.addEventListener('keydown', (event) => {
        let nextIndex = null;
        if (event.key === 'ArrowRight' || event.key === 'ArrowDown') nextIndex = (index + 1) % tabs.length;
        if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') nextIndex = (index - 1 + tabs.length) % tabs.length;
        if (event.key === 'Home') nextIndex = 0;
        if (event.key === 'End') nextIndex = tabs.length - 1;
        if (nextIndex === null) return;
        event.preventDefault();
        tabs[nextIndex].focus();
        activateTab(tabs[nextIndex]);
      });
    });
  });

  const cards = Array.from(document.querySelectorAll('.showcase-card'));
  const videos = cards
    .map((card) => card.querySelector('video'))
    .filter(Boolean);

  function playCard(card) {
    const video = card.querySelector('video');
    card.classList.add('is-active');
    if (video) {
      video.play().catch(() => {
        // Browsers may decline playback until the first user gesture.
      });
    }
  }

  function pauseCard(card) {
    const video = card.querySelector('video');
    card.classList.remove('is-active');
    if (video) video.pause();
  }

  cards.forEach((card) => {
    card.addEventListener('pointerenter', () => playCard(card));
    card.addEventListener('pointerleave', () => pauseCard(card));
    card.addEventListener('focusin', () => playCard(card));
    card.addEventListener('focusout', () => pauseCard(card));
  });

  // Expand every showcase card in a shared, keyboard-accessible lightbox.
  const lightbox = document.createElement('div');
  lightbox.className = 'showcase-lightbox';
  lightbox.hidden = true;
  lightbox.setAttribute('role', 'dialog');
  lightbox.setAttribute('aria-modal', 'true');
  lightbox.setAttribute('aria-label', 'Expanded showcase card');
  lightbox.innerHTML = `
    <div class="showcase-lightbox-inner">
      <button type="button" class="showcase-lightbox-close" data-lightbox-close aria-label="Close enlarged card">×</button>
      <div class="showcase-lightbox-media"></div>
      <div class="showcase-lightbox-caption">
        <span class="showcase-lightbox-label"></span>
        <h2 class="showcase-lightbox-title"></h2>
        <div class="showcase-lightbox-link"></div>
      </div>
    </div>
  `;
  document.body.appendChild(lightbox);

  const lightboxMedia = lightbox.querySelector('.showcase-lightbox-media');
  const lightboxLabel = lightbox.querySelector('.showcase-lightbox-label');
  const lightboxTitle = lightbox.querySelector('.showcase-lightbox-title');
  const lightboxLink = lightbox.querySelector('.showcase-lightbox-link');
  const lightboxClose = lightbox.querySelector('[data-lightbox-close]');
  let previousFocus = null;

  function closeLightbox() {
    if (lightbox.hidden) return;
    lightbox.classList.remove('is-open');
    document.body.classList.remove('showcase-lightbox-open');
    const focusTarget = previousFocus;
    previousFocus = null;
    window.setTimeout(() => {
      if (!lightbox.classList.contains('is-open')) {
        lightbox.hidden = true;
        lightboxMedia.replaceChildren();
      }
    }, 180);
    if (focusTarget && typeof focusTarget.focus === 'function') focusTarget.focus();
  }

  function openLightbox(card, { showOriginalLink = true } = {}) {
    const source = card.querySelector('img, video, canvas');
    if (!source) return;

    previousFocus = document.activeElement;
    lightboxMedia.replaceChildren();
    let media;
    if (source.tagName === 'CANVAS') {
      let snapshot = '';
      try { snapshot = source.toDataURL('image/png'); } catch (error) { /* ignore unreadable canvas */ }
      if (!snapshot) return;
      media = Object.assign(new Image(), {
        src: snapshot,
        alt: source.getAttribute('aria-label') || 'Expanded showcase render',
      });
    } else {
      media = source.cloneNode(true);
    }
    media.className = 'showcase-lightbox-media-element';
    media.removeAttribute('loading');
    media.removeAttribute('aria-hidden');
    if (media.tagName === 'VIDEO') {
      media.controls = true;
      media.autoplay = true;
      media.muted = true;
      media.loop = true;
      media.playsInline = true;
    }
    lightboxMedia.appendChild(media);

    const directSpan = Array.from(card.children).find((child) => child.tagName === 'SPAN');
    const directTitle = Array.from(card.children).find((child) => child.tagName === 'STRONG');
    const sourceLabel = card.querySelector('.showcase-card-overlay > span') || directSpan;
    const sourceTitle = card.querySelector('.showcase-card-overlay h3') || directTitle;
    lightboxLabel.textContent = sourceLabel ? sourceLabel.textContent.trim() : '';
    lightboxTitle.textContent = sourceTitle
      ? sourceTitle.textContent.trim()
      : (source.alt || 'Showcase');
    lightboxLink.replaceChildren();

    const href = card.dataset.originalUrl || (showOriginalLink ? card.getAttribute('href') : '');
    if (href) {
      const link = document.createElement('a');
      link.className = 'showcase-lightbox-original';
      link.href = href;
      link.target = card.dataset.originalTarget || card.getAttribute('target') || '_blank';
      link.rel = card.getAttribute('rel') || 'noopener noreferrer';
      link.textContent = card.dataset.originalUrl ? 'Tutorial ↗' : 'Open original tutorial ↗';
      lightboxLink.appendChild(link);
    }

    lightbox.hidden = false;
    document.body.classList.add('showcase-lightbox-open');
    requestAnimationFrame(() => lightbox.classList.add('is-open'));
    lightboxClose.focus();
    if (media.tagName === 'VIDEO') media.play().catch(() => {});
  }

  cards.forEach((card) => {
    const title = card.querySelector('.showcase-card-overlay h3')?.textContent.trim();
    card.setAttribute('aria-haspopup', 'dialog');
    if (!card.matches('a, button, [tabindex]')) {
      card.setAttribute('role', 'button');
      card.tabIndex = 0;
    }
    if (!card.getAttribute('aria-label') && title) {
      card.setAttribute('aria-label', `Open ${title} enlarged`);
    }
    card.addEventListener('click', (event) => {
      if (card.matches('a[href]') && (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey)) return;
      event.preventDefault();
      openLightbox(card);
    });
    card.addEventListener('keydown', (event) => {
      if (event.key === ' ' || event.key === 'Enter') {
        event.preventDefault();
        openLightbox(card);
      }
    });
  });

  // Keep the real Tutorial link independent from the card lightbox handler.
  // Do not preventDefault here: the anchor must open Bilibili in a new tab.
  const originalLinks = document.querySelectorAll('.showcase-card-original-link');
  originalLinks.forEach((link) => {
    link.addEventListener('click', (event) => event.stopPropagation());
    link.addEventListener('keydown', (event) => event.stopPropagation());
  });

  // Workflow media should enlarge in the same lightbox instead of opening
  // the linked image in a new page. Keep this scope limited to the two
  // featured workflows.
  const workflowMedia = Array.from(document.querySelectorAll(
    '#workflow-stained-glass .workflow-media, #workflow-stained-glass .workflow-result, '
    + '#workflow-chromatic-duck .workflow-media, #workflow-chromatic-duck .workflow-result',
  ));
  workflowMedia.forEach((media) => {
    media.setAttribute('aria-haspopup', 'dialog');
    media.addEventListener('click', (event) => {
      event.preventDefault();
      openLightbox(media, { showOriginalLink: false });
    });
    media.addEventListener('keydown', (event) => {
      if (event.key !== ' ' && event.key !== 'Enter') return;
      event.preventDefault();
      openLightbox(media, { showOriginalLink: false });
    });
  });

  lightbox.addEventListener('click', (event) => {
    if (event.target === lightbox || event.target.closest('[data-lightbox-close]')) closeLightbox();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !lightbox.hidden) closeLightbox();
  });

  if ('IntersectionObserver' in window) {
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) entry.target.pause();
      });
    }, { rootMargin: '160px 0px', threshold: 0.01 });

    videos.forEach((video) => observer.observe(video));
  }
})();
