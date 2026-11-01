function eventDone(target, event, signal) {
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      target.removeEventListener(event, success);
      target.removeEventListener('error', failure);
      signal.removeEventListener('abort', aborted);
    };
    const success = () => { cleanup(); resolve(); };
    const failure = () => { cleanup(); reject(new Error('영상 데이터를 재생하지 못했습니다.')); };
    const aborted = () => { cleanup(); reject(signal.reason); };
    if (signal.aborted) return aborted();
    target.addEventListener(event, success, { once: true });
    target.addEventListener('error', failure, { once: true });
    signal.addEventListener('abort', aborted, { once: true });
  });
}

export class SegmentPlayer {
  constructor(video, item, load, onMessage) {
    this.video = video;
    this.item = item;
    this.load = load;
    this.onMessage = onMessage;
    this.life = new AbortController();
    this.request = new AbortController();
    this.epoch = 0;
    this.states = [];
    this.pumping = false;
    this.failed = false;
  }

  async start() {
    const Source = globalThis.MediaSource;
    if (!Source || !this.item.tracks.every(track => Source.isTypeSupported(track.mimeCodec))) {
      throw new Error('이 브라우저에서는 이 AV1 영상을 재생할 수 없습니다. AV1과 MediaSource를 지원하는 브라우저에서 열어주세요.');
    }
    this.media = new Source();
    this.url = URL.createObjectURL(this.media);
    const opened = eventDone(this.media, 'sourceopen', this.life.signal);
    this.video.src = this.url;
    this.video.load();
    await opened;
    for (const track of this.item.tracks) {
      const state = { track, buffer: this.media.addSourceBuffer(track.mimeCodec), appended: new Set() };
      state.buffer.mode = 'segments';
      this.states.push(state);
    }
    await Promise.all(this.states.map(async state => {
      const bytes = await this.load(state.track.init, this.request.signal);
      await this.append(state, bytes);
    }));
    this.media.duration = this.item.duration;
    const options = { signal: this.life.signal };
    this.video.addEventListener('timeupdate', () => this.kick(), options);
    this.video.addEventListener('waiting', () => this.kick(), options);
    this.video.addEventListener('seeking', () => {
      this.epoch++;
      this.request.abort();
      this.request = new AbortController();
      this.kick();
    }, options);
    this.video.addEventListener('error', () => {
      this.failed = true;
      this.onMessage('영상 재생에 실패했습니다. 다른 브라우저에서 시도하거나 다시 열어주세요.', true);
    }, options);
    this.video.addEventListener('playing', () => this.onMessage(''), options);
    await this.kick();
    if (!this.life.signal.aborted && !this.failed) {
      try { await this.video.play(); }
      catch { this.onMessage('준비되었습니다. 재생 버튼을 눌러주세요.'); }
    }
  }

  async append(state, bytes) {
    if (this.life.signal.aborted) throw this.life.signal.reason;
    if (state.buffer.updating) await eventDone(state.buffer, 'updateend', this.life.signal);
    // updateend is queued asynchronously; sync append errors must not leave a dangling waiter.
    state.buffer.appendBuffer(bytes);
    await eventDone(state.buffer, 'updateend', this.life.signal);
  }

  async remove(state, start, end) {
    if (end <= start || this.life.signal.aborted) return;
    if (state.buffer.updating) await eventDone(state.buffer, 'updateend', this.life.signal);
    state.buffer.remove(start, end);
    await eventDone(state.buffer, 'updateend', this.life.signal);
    for (const segment of state.track.segments) {
      if (segment.start < end - 0.001 && segment.start + segment.duration > start + 0.001) state.appended.delete(segment.index);
    }
  }

  async trim(state, time, aggressive = false) {
    const segments = state.track.segments;
    const behind = Math.max(0, time - (aggressive ? 2 : 8));
    const boundary = segments.filter(s => s.start + s.duration <= behind).at(-1);
    if (boundary && state.buffer.buffered.length && state.buffer.buffered.start(0) < boundary.start + boundary.duration - 0.01) {
      await this.remove(state, 0, boundary.start + boundary.duration);
    }
    const ahead = segments.find(s => s.start >= time + (aggressive ? 8 : 24));
    if (ahead && state.buffer.buffered.length && state.buffer.buffered.end(state.buffer.buffered.length - 1) > ahead.start) {
      await this.remove(state, ahead.start, this.item.duration + 1);
    }
  }

  async fill(state, epoch, time, signal) {
    await this.trim(state, time);
    for (const segment of state.track.segments) {
      if (epoch !== this.epoch || this.life.signal.aborted) return;
      if (segment.start + segment.duration <= time + 0.001 || segment.start > time + 12 || state.appended.has(segment.index)) continue;
      const bytes = await this.load(segment, signal);
      if (epoch !== this.epoch || this.life.signal.aborted) return;
      try { await this.append(state, bytes); }
      catch (error) {
        if (error.name !== 'QuotaExceededError') throw error;
        await this.trim(state, time, true);
        await this.append(state, bytes);
      }
      state.appended.add(segment.index);
    }
  }

  async kick() {
    this.dirty = true;
    if (this.pumping || this.life.signal.aborted || this.failed) return;
    this.pumping = true;
    try {
      while (this.dirty && !this.life.signal.aborted && !this.failed) {
        this.dirty = false;
        const epoch = this.epoch, time = this.video.currentTime;
        const results = await Promise.allSettled(this.states.map(state => this.fill(state, epoch, time, this.request.signal)));
        if (epoch !== this.epoch || this.life.signal.aborted) continue;
        const failure = results.find(result => result.status === 'rejected');
        if (failure) throw failure.reason;
        if (this.media.readyState === 'open' && this.states.every(state => !state.buffer.updating && state.appended.has(state.track.segments.length - 1))) {
          this.media.endOfStream();
        }
      }
    } catch (error) {
      if (!this.life.signal.aborted) {
        this.failed = true;
        this.request.abort();
        this.onMessage(error?.message || '영상 조각을 불러오지 못했습니다.', true);
      }
    } finally {
      this.pumping = false;
    }
  }

  destroy() {
    this.life.abort();
    this.request.abort();
    this.video.pause();
    this.video.removeAttribute('src');
    this.video.load();
    if (this.url) URL.revokeObjectURL(this.url);
    this.states = [];
  }
}
