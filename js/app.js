import { importKey, encryptedLoader } from './crypto.js';
import { validateManifest } from './manifest.js';
import { createGallery } from './gallery.js';

const form = document.querySelector('#unlock-form');
const input = document.querySelector('#key');
const message = document.querySelector('#unlock-message');
const button = form.querySelector('button');
const base = new URL('./', location.href);
const albumId = base.pathname.split('/').filter(Boolean).at(-1);
let busy = false;

async function unlock(value) {
  if (busy) return;
  busy = true;
  button.disabled = true;
  message.textContent = '앨범을 열고 있습니다…';
  try {
    const key = await importKey(value.trim());
    value = '';
    input.value = '';
    const loader = encryptedLoader(base, albumId, key);
    const buffer = await loader({ path: 'data/manifest.enc' });
    let manifest;
    try { manifest = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(buffer)); }
    catch { throw new Error('앨범 목록을 읽을 수 없습니다.'); }
    validateManifest(manifest, albumId);
    createGallery(manifest, loader);
    document.querySelector('#unlock').hidden = true;
    document.querySelector('#album').hidden = false;
    document.querySelector('#album-title').focus();
  } catch (error) {
    message.textContent = error.message;
    input.focus();
  } finally {
    input.value = '';
    busy = false;
    button.disabled = false;
  }
}

form.addEventListener('submit', event => {
  event.preventDefault();
  void unlock(input.value);
});

let incoming = new URLSearchParams(location.hash.slice(1)).get('key');
if (location.hash) history.replaceState(null, '', location.pathname + location.search);
if (incoming) {
  void unlock(incoming);
  incoming = null;
}
