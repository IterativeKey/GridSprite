/* png.js — minimal PNG encoder for the pipeline's RGBA buffers.
 *
 * Writes 8-bit RGBA, filter 0 on every row, zlib-compressed with the
 * native CompressionStream("deflate"). Pixels go in byte-for-byte: no
 * canvas, so no premultiplied alpha or color management touching them.
 * CRC32 is shared with zip.js.
 *
 * API: encodePNG(rgba, width, height) -> Promise<Uint8Array>
 */
"use strict";

async function encodePNG(rgba, width, height){
  const crc32 = typeof zipCRC32 === "function"
    ? zipCRC32 : require("./zip.js").crc32;

  const stride = width * 4;
  const raw = new Uint8Array((stride + 1) * height);
  for (let y = 0; y < height; y++){
    raw.set(rgba.subarray(y * stride, (y + 1) * stride), y * (stride + 1) + 1);
  }
  const idat = new Uint8Array(await new Response(
    new Blob([raw]).stream().pipeThrough(new CompressionStream("deflate"))
  ).arrayBuffer());

  const ihdr = new Uint8Array(13);
  const dv = new DataView(ihdr.buffer);
  dv.setUint32(0, width); dv.setUint32(4, height);
  ihdr.set([8, 6, 0, 0, 0], 8);   // 8-bit depth, RGBA, deflate, filter 0, no interlace

  const chunks = [["IHDR", ihdr], ["IDAT", idat], ["IEND", new Uint8Array(0)]];
  const total = 8 + chunks.reduce((n, [, d]) => n + 12 + d.length, 0);
  const out = new Uint8Array(total);
  const odv = new DataView(out.buffer);
  out.set([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]);
  let at = 8;
  for (const [type, data] of chunks){
    odv.setUint32(at, data.length);
    const body = out.subarray(at + 4, at + 8 + data.length);
    body.set(new TextEncoder().encode(type));
    body.set(data, 4);
    odv.setUint32(at + 8 + data.length, crc32(body));
    at += 12 + data.length;
  }
  return out;
}

if (typeof window !== "undefined") window.encodePNG = encodePNG;
if (typeof module === "object" && module.exports) module.exports = encodePNG;
