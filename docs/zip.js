/* zip.js — minimal ZIP writer ("store" method, no compression).
 *
 * Pipeline exports are already small (SVG text, tiny PNGs), so stored
 * entries keep this dependency-free and ~100 lines instead of pulling a
 * compression library. Produces a standard ZIP archive readable everywhere.
 *
 * API: makeZip(entries) -> Uint8Array   (synchronous)
 *   entries: [{ name: "sprite.svg", bytes?: Uint8Array, text?: string }, ...]
 */
"use strict";

function zipCRC32(buf){
  if (!zipCRC32.table){
    zipCRC32.table = new Int32Array(256);
    for (let n = 0; n < 256; n++){
      let c = n;
      for (let k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
      zipCRC32.table[n] = c;
    }
  }
  let crc = -1;
  for (let i = 0; i < buf.length; i++){
    crc = zipCRC32.table[(crc ^ buf[i]) & 0xFF] ^ (crc >>> 8);
  }
  return (crc ^ -1) >>> 0;
}

function makeZip(entries){
  const localParts = [];    // little-endian byte arrays
  const centralParts = [];
  let offset = 0;

  for (const e of entries){
    const nameBytes = new TextEncoder().encode(e.name);
    const data = e.bytes instanceof Uint8Array
      ? e.bytes
      : new TextEncoder().encode(e.text);
    const crc = zipCRC32(data);
    const size = data.length;
    const dosTime = 0, dosDate = 0x21;   // 1980-01-01; deterministic output

    localParts.push([
      0x50, 0x4B, 0x03, 0x04,             // local file header signature
      0x14, 0x00,                         // version needed: 2.0
      0x00, 0x00,                         // flags
      0x00, 0x00,                         // method: store
      ...u16(dosTime), ...u16(dosDate),
      ...u32(crc), ...u32(size), ...u32(size),
      ...u16(nameBytes.length), 0x00, 0x00,
      ...nameBytes, ...data,
    ]);

    centralParts.push([
      0x50, 0x4B, 0x01, 0x02,             // central directory header
      0x14, 0x00, 0x14, 0x00,             // versions made by / needed
      0x00, 0x00, 0x00, 0x00,             // flags, method: store
      ...u16(dosTime), ...u16(dosDate),
      ...u32(crc), ...u32(size), ...u32(size),
      ...u16(nameBytes.length), 0x00, 0x00,   // name len, extra len
      0x00, 0x00,                         // comment length
      0x00, 0x00,                         // disk number start
      0x00, 0x00,                         // internal file attributes
      0x00, 0x00, 0x00, 0x00,             // external file attributes
      ...u32(offset),                     // local header offset
      ...nameBytes,
    ]);

    offset += 30 + nameBytes.length + size;
  }

  const centralOffset = offset;
  let centralSize = 0;
  for (const p of centralParts) centralSize += p.length;

  const end = [
    0x50, 0x4B, 0x05, 0x06,               // end of central directory
    0x00, 0x00, 0x00, 0x00,               // disk numbers
    ...u16(centralParts.length), ...u16(centralParts.length),
    ...u32(centralSize), ...u32(centralOffset),
    0x00, 0x00,                           // comment length
  ];

  const allParts = [...localParts, ...centralParts, end];
  const total = allParts.reduce((n, p) => n + p.length, 0);
  const out = new Uint8Array(total);
  let at = 0;
  for (const p of allParts){ out.set(p, at); at += p.length; }
  return out;
}

function u16(v){ return [v & 255, (v >>> 8) & 255]; }
function u32(v){ return [v & 255, (v >>> 8) & 255, (v >>> 16) & 255, (v >>> 24) & 255]; }

if (typeof window !== "undefined") window.makeZip = makeZip;
if (typeof module === "object" && module.exports) module.exports = makeZip;
