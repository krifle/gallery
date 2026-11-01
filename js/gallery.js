import { SegmentPlayer } from './player.js';

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export function createGallery(manifest, load) {
  const tabs = document.querySelector('#day-tabs');
  const grid = document.querySelector('#media-grid');
  const dialog = document.querySelector('#viewer');
  const stage = document.querySelector('#viewer-stage');
  const viewerMessage = document.querySelector('#viewer-message');
  const retryViewer = document.querySelector('#viewer-retry');
  const info = document.querySelector('#viewer-info');
  const previous = document.querySelector('#viewer-prev'), next = document.querySelector('#viewer-next');
  let activeDay = 0, activeItem = 0, player, viewerAbort, viewerToken = 0, opener;
  let dayAbort = new AbortController(), observer;
  const thumbnails = new Map(), viewerURLs = new Set();
  let jobs = [], running = 0;

  document.querySelector('#album-title').textContent = manifest.title || '여행 기록';
  document.querySelector('#album-count').textContent = `${new Set(manifest.days.map(day => day.date)).size}일의 기록 · ${manifest.days.reduce((sum, day) => sum + day.media.length, 0)}개의 순간`;
  document.querySelector('#album').hidden = false;

  function pumpThumbnails() {
    while (running < 3 && jobs.length) {
      const job = jobs.shift();
      if (job.signal.aborted) continue;
      running++;
      void job.run().finally(() => { running--; pumpThumbnails(); });
    }
  }

  function setMessage(text, failed = false) {
    viewerMessage.textContent = text;
    retryViewer.hidden = !failed;
  }

  function resetViewer() {
    viewerToken++;
    viewerAbort?.abort();
    player?.destroy();
    player = undefined;
    stage.replaceChildren();
    for (const url of viewerURLs) URL.revokeObjectURL(url);
    viewerURLs.clear();
    dialog.classList.remove('is-playing', 'is-video');
    setMessage('');
  }

  async function showItem(index) {
    resetViewer();
    const token = viewerToken;
    activeItem = index;
    const day = manifest.days[activeDay], item = day.media[index];
    viewerAbort = new AbortController();
    const signal = viewerAbort.signal;
    document.querySelector('#viewer-count').textContent = `${index + 1} / ${day.media.length}`;
    document.querySelector('#viewer-title').textContent = item.title || (item.type === 'video' ? '영상' : '사진');
    document.querySelector('#viewer-description').textContent = item.description || '';
    document.querySelector('#viewer-exif').textContent = item.EXIF || '';
    info.hidden = ![item.title, item.description, item.EXIF].some(Boolean);
    info.open = item.type === 'image';
    previous.disabled = index === 0;
    next.disabled = index === day.media.length - 1;
    if (!dialog.open) dialog.showModal();
    setMessage(item.type === 'video' ? '영상을 준비하고 있습니다…' : '사진을 불러오고 있습니다…');
    try {
      if (item.type === 'image') {
        const buffer = await load(item.image, signal);
        if (token !== viewerToken) return;
        const url = URL.createObjectURL(new Blob([buffer], { type: 'image/webp' }));
        viewerURLs.add(url);
        const image = element('img', 'viewer-image');
        image.alt = item.title || '여행 사진';
        image.src = url;
        await image.decode();
        if (token !== viewerToken) return;
        stage.append(image);
        setMessage('');
      } else {
        dialog.classList.add('is-video');
        const video = element('video', 'viewer-video');
        video.controls = true;
        video.playsInline = true;
        video.preload = 'none';
        video.setAttribute('aria-label', item.title || '여행 영상');
        const poster = thumbnails.get(item.id);
        if (poster) video.poster = poster;
        stage.append(video);
        video.addEventListener('playing', () => { dialog.classList.add('is-playing'); info.open = false; }, { signal });
        video.addEventListener('pause', () => dialog.classList.remove('is-playing'), { signal });
        const currentPlayer = new SegmentPlayer(video, item, load, (message, failed) => {
          if (token === viewerToken) setMessage(message, failed);
        });
        player = currentPlayer;
        await currentPlayer.start();
      }
    } catch (error) {
      if (token === viewerToken && !signal.aborted) setMessage(error?.message || '파일을 표시하지 못했습니다.', true);
    }
  }

  function closeViewer() {
    // A queued close event may arrive after the user has already reopened the dialog.
    if (dialog.open) return;
    resetViewer();
    opener?.focus();
  }
  dialog.addEventListener('close', closeViewer);
  document.querySelector('#viewer-close').addEventListener('click', () => dialog.close());
  previous.addEventListener('click', () => { if (activeItem > 0) void showItem(activeItem - 1); });
  next.addEventListener('click', () => { if (activeItem + 1 < manifest.days[activeDay].media.length) void showItem(activeItem + 1); });
  retryViewer.addEventListener('click', () => void showItem(activeItem));
  dialog.addEventListener('keydown', event => {
    if (event.target.tagName === 'VIDEO') return;
    if (event.key === 'ArrowLeft' && !previous.disabled) { event.preventDefault(); void showItem(activeItem - 1); }
    if (event.key === 'ArrowRight' && !next.disabled) { event.preventDefault(); void showItem(activeItem + 1); }
  });

  function showDay(index, focus = false) {
    if (dialog.open) dialog.close();
    dayAbort.abort();
    dayAbort = new AbortController();
    const signal = dayAbort.signal;
    observer?.disconnect();
    jobs = [];
    for (const url of thumbnails.values()) URL.revokeObjectURL(url);
    thumbnails.clear();
    activeDay = index;
    [...tabs.children].forEach((tab, i) => {
      tab.setAttribute('aria-selected', String(index === i));
      tab.tabIndex = index === i ? 0 : -1;
    });
    if (focus) tabs.children[index].focus();
    const day = manifest.days[index];
    document.querySelector('#day-heading').textContent = day.title || day.date;
    document.querySelector('#day-date').textContent = day.date.replaceAll('-', '.');
    document.querySelector('#day-description').textContent = day.description || '';
    document.querySelector('#day-description').hidden = !day.description;
    document.querySelector('#day-panel').setAttribute('aria-labelledby', `day-tab-${index}`);
    document.querySelector('#empty-day').hidden = day.media.length > 0;
    grid.replaceChildren();
    observer = new IntersectionObserver(entries => {
      for (const entry of entries) {
        if (entry.isIntersecting) { observer.unobserve(entry.target); entry.target.loadThumbnail(); }
      }
    }, { rootMargin: '300px' });
    day.media.forEach((item, itemIndex) => {
      const tile = element('article', `tile ${item.height > item.width ? 'portrait' : 'landscape'}`);
      const open = element('button', 'tile-open');
      open.type = 'button';
      open.setAttribute('aria-label', `${item.title || `${item.type === 'video' ? '영상' : '사진'} ${itemIndex + 1}`} 크게 보기`);
      const picture = element('div', 'tile-picture');
      const image = element('img');
      image.alt = '';
      image.decoding = 'async';
      picture.append(image);
      if (item.type === 'video') {
        const play = element('span', 'play-mark', '▶');
        play.setAttribute('aria-hidden', 'true');
        picture.append(play);
        const label = element('span', 'video-label', `${Math.floor(item.duration / 60)}:${String(Math.floor(item.duration % 60)).padStart(2, '0')}`);
        label.setAttribute('aria-hidden', 'true');
        picture.append(label);
      }
      open.append(picture);
      if (item.title) open.append(element('span', 'tile-caption', item.title));
      open.addEventListener('click', () => { opener = open; void showItem(itemIndex); });
      const retry = element('button', 'thumbnail-retry', '썸네일 다시 불러오기');
      retry.type = 'button';
      retry.hidden = true;
      let pending = false;
      tile.loadThumbnail = () => {
        if (pending || signal.aborted) return;
        pending = true;
        retry.hidden = true;
        jobs.push({ signal, run: async () => {
          let url;
          try {
            const buffer = await load(item.thumbnail, signal);
            if (signal.aborted) return;
            url = URL.createObjectURL(new Blob([buffer], { type: 'image/webp' }));
            image.src = url;
            await image.decode();
            if (signal.aborted) { URL.revokeObjectURL(url); return; }
            thumbnails.set(item.id, url);
            tile.classList.add('loaded');
          } catch {
            if (url) URL.revokeObjectURL(url);
            if (!signal.aborted) retry.hidden = false;
          } finally { pending = false; }
        } });
        pumpThumbnails();
      };
      retry.addEventListener('click', tile.loadThumbnail);
      tile.append(open, retry);
      grid.append(tile);
      observer.observe(tile);
    });
  }

  manifest.days.forEach((day, index) => {
    const tab = element('button', 'day-tab', day.title || day.date);
    tab.type = 'button';
    tab.id = `day-tab-${index}`;
    tab.setAttribute('role', 'tab');
    tab.setAttribute('aria-controls', 'day-panel');
    tab.addEventListener('click', () => showDay(index));
    tab.addEventListener('keydown', event => {
      const indices = { ArrowRight: (index + 1) % manifest.days.length, ArrowLeft: (index - 1 + manifest.days.length) % manifest.days.length,
        Home: 0, End: manifest.days.length - 1 };
      if (event.key in indices) { event.preventDefault(); showDay(indices[event.key], true); }
    });
    tabs.append(tab);
  });
  showDay(0);
  window.addEventListener('pagehide', () => {
    resetViewer();
    dayAbort.abort();
    observer?.disconnect();
    thumbnails.forEach(url => URL.revokeObjectURL(url));
    thumbnails.clear();
  }, { once: true });
}
