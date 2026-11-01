// Validate both decrypted data and locally generated preview data before rendering.
export function validateManifest(data, albumId, { plain = false } = {}) {
  const fail = () => { throw new Error('지원하지 않거나 손상된 앨범 목록입니다.'); };
  const object = value => value && typeof value === 'object' && !Array.isArray(value);
  const number = value => typeof value === 'number' && Number.isFinite(value);
  const text = obj => {
    for (const field of ['title', 'description', 'EXIF']) {
      if (obj[field] !== undefined && typeof obj[field] !== 'string') fail();
    }
  };
  const paths = new Map();
  const reference = ref => {
    const pattern = plain ? /^\.prepare\/(converted|segments)\/[a-f0-9]{64}\/[A-Za-z0-9_.-]+$/ : /^media\/[a-f0-9]{32}\.enc$/;
    if (!object(ref) || !pattern.test(ref.path) || !Number.isSafeInteger(ref.bytes) || ref.bytes < (plain ? 1 : 32) || ref.bytes > 100_000_000) fail();
    if (paths.has(ref.path) && paths.get(ref.path) !== ref.bytes) fail();
    paths.set(ref.path, ref.bytes);
  };
  if (!object(data) || data.schemaVersion !== 1 || data.albumId !== albumId || !/^[a-f0-9]{32}$/.test(data.buildId) || !Array.isArray(data.days) || !data.days.length) fail();
  text(data);
  const ids = new Set();
  for (const day of data.days) {
    if (!object(day) || typeof day.date !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(day.date) ||
        Number.isNaN(Date.parse(day.date)) || new Date(day.date).toISOString().slice(0, 10) !== day.date || !Array.isArray(day.media)) fail();
    text(day);
    for (const item of day.media) {
      if (!object(item) || !/^[a-f0-9]{32}$/.test(item.id) || ids.has(item.id) || !['image', 'video'].includes(item.type)) fail();
      ids.add(item.id);
      text(item);
      if (![item.width, item.height].every(n => Number.isInteger(n) && n > 0 && n <= 30000)) fail();
      reference(item.thumbnail);
      if (item.type === 'image') {
        if (item.mime !== 'image/webp') fail();
        reference(item.image);
      } else {
        if (!number(item.duration) || item.duration <= 0 || !number(item.fps) || item.fps <= 0 || !Array.isArray(item.tracks) || ![1, 2].includes(item.tracks.length)) fail();
        const kinds = new Set();
        for (const track of item.tracks) {
          if (!object(track) || !['video', 'audio'].includes(track.kind) || kinds.has(track.kind) ||
              !/^(video|audio)\/mp4; codecs="[A-Za-z0-9., ]+"$/.test(track.mimeCodec) ||
              !track.mimeCodec.startsWith(track.kind + '/') || !Array.isArray(track.segments) || !track.segments.length) fail();
          kinds.add(track.kind);
          reference(track.init);
          let end = 0;
          track.segments.forEach((segment, index) => {
            reference(segment);
            if (segment.index !== index || !number(segment.start) || !number(segment.duration) || segment.duration <= 0 ||
                Math.abs(segment.start - end) > 0.1 || segment.start + segment.duration > item.duration + 0.1) fail();
            end = segment.start + segment.duration;
          });
        }
        if (!kinds.has('video')) fail();
      }
    }
  }
  if ([...paths.values()].reduce((a, b) => a + b, 0) > 100_000_000) fail();
  return data;
}
