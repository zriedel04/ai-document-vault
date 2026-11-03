/** What the image viewer shows, and the sizes above which it asks before showing it. */

/**
 * The media types the viewer shows: the two image kinds the importer accepts (PNG and JPEG,
 * `vault.ingest.validate`). The blob is labelled with one of these, never with what the response
 * said, so the address opened alone is never a page (HTML) or a picture with scripts (SVG).
 */
export const IMAGE_TYPES = ["image/png", "image/jpeg"] as const;
export type ImageType = (typeof IMAGE_TYPES)[number];

export function isImageType(mime: string): mime is ImageType {
  return (IMAGE_TYPES as readonly string[]).includes(mime);
}

/**
 * Files above this size are fetched only when asked: the whole file is held in the page's memory
 * as a blob while it is shown, and a file this large is usually a scan of many megapixels.
 */
export const LARGE_IMAGE_BYTES = 32 * 1024 * 1024;
/**
 * Images with more pixels than this are not drawn until asked. Browsers read an image's size from
 * its header when it loads, and generally decode it to pixels (4 bytes each) only to draw it, so a
 * small file can still need gigabytes then. The viewer draws nothing until it knows the size. 50
 * megapixels is about 200 MB once decoded.
 */
export const LARGE_IMAGE_PIXELS = 50_000_000;
/** The zoom steps, as a share of the image's own size (1 is 100%). */
export const ZOOM_STEPS = [0.1, 0.25, 0.5, 0.75, 1, 1.5, 2, 3, 4] as const;
