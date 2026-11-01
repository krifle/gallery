const encoder = new TextEncoder();
const resourcePattern = /^media\/[a-f0-9]{32}\.enc$/;

export function resourcePath(path) {
  if (path !== 'data/manifest.enc' && !resourcePattern.test(path)) {
    throw new Error('올바르지 않은 리소스 경로입니다.');
  }
  return path;
}

export async function importKey(text) {
  if (!globalThis.crypto?.subtle) throw new Error('HTTPS 또는 localhost에서 갤러리를 열어주세요.');
  if (!/^[A-Za-z0-9_-]{43}$/.test(text)) throw new Error('43자의 공유 키를 입력해주세요.');
  const bytes = Uint8Array.from(atob(text.replace(/-/g, '+').replace(/_/g, '/') + '='), c => c.charCodeAt(0));
  const canonical = btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  if (bytes.length !== 32 || canonical !== text) throw new Error('올바른 Base64URL 키가 아닙니다.');
  return crypto.subtle.importKey('raw', bytes, 'AES-GCM', false, ['decrypt']);
}

export async function decrypt(data, key, albumId, path) {
  resourcePath(path);
  if (!/^[A-Za-z0-9][A-Za-z0-9_-]*$/.test(albumId)) throw new Error('잘못된 앨범 ID입니다.');
  const bytes = new Uint8Array(data);
  if (bytes.length < 32 || String.fromCharCode(...bytes.slice(0, 4)) !== 'GAL1') {
    throw new Error('갤러리 데이터 형식이 올바르지 않습니다.');
  }
  try {
    return await crypto.subtle.decrypt({ name: 'AES-GCM', iv: bytes.slice(4, 16), tagLength: 128,
      additionalData: encoder.encode(`gallery:v1|${albumId}|${path}`) }, key, bytes.slice(16));
  } catch {
    throw new Error('키가 올바르지 않거나 데이터가 손상되었습니다.');
  }
}

export async function fetchBuffer(url, { signal, bytes, cache = 'default' } = {}) {
  const timeout = new AbortController();
  const timer = setTimeout(() => timeout.abort(new Error('연결이 오래 걸립니다. 다시 시도해주세요.')), 45000);
  const abort = () => timeout.abort(signal.reason);
  if (signal?.aborted) abort();
  signal?.addEventListener('abort', abort, { once: true });
  try {
    const response = await fetch(url, { signal: timeout.signal, cache, referrerPolicy: 'no-referrer' });
    if (!response.ok) {
      throw new Error(response.status === 404 ? '파일을 찾을 수 없습니다. 앨범을 새로고침해주세요.' : `파일을 불러오지 못했습니다 (${response.status}).`);
    }
    const data = await response.arrayBuffer();
    if ((bytes !== undefined && data.byteLength !== bytes) || data.byteLength > 100_000_000) {
      throw new Error('파일 크기가 일치하지 않습니다. 앨범을 새로고침해주세요.');
    }
    return data;
  } catch (error) {
    if (signal?.aborted) throw signal.reason;
    if (timeout.signal.aborted) throw timeout.signal.reason;
    if (error instanceof TypeError) throw new Error('연결을 확인한 뒤 다시 시도해주세요.');
    throw error;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', abort);
  }
}

export function encryptedLoader(base, albumId, key) {
  return async (ref, signal) => {
    const path = resourcePath(ref.path);
    const data = await fetchBuffer(new URL(path, base), { signal, bytes: ref.bytes,
      cache: path === 'data/manifest.enc' ? 'no-store' : 'default' });
    if (signal?.aborted) throw signal.reason;
    return decrypt(data, key, albumId, path);
  };
}
