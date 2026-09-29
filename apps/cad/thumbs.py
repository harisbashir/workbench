"""Small preview pictures of 3D models for lists and cards (drawn on the server with Pillow)."""
import io

import numpy as np
from PIL import Image

MAX_TRIS = 60_000


def render_png(mesh, size=(480, 320), background=(234, 238, 243)):
    """Isometric-ish view, flat shading, painter's algorithm. Returns PNG bytes."""
    scale_up = 1.5
    W, H = int(size[0] * scale_up), int(size[1] * scale_up)
    tris, cols = [], []
    for p in mesh.parts:
        if not len(p.triangles):
            continue
        tris.append(p.vertices[p.triangles].astype(np.float64))
        cols.append(np.tile(np.array(p.color[:3]), (len(p.triangles), 1)))
    img = Image.new("RGB", (W, H), background)
    if not tris:
        return _png(img, size)
    T = np.concatenate(tris)
    C = np.concatenate(cols)
    if len(T) > MAX_TRIS:  # keep the biggest triangles: they're what you see at thumbnail size
        area = np.linalg.norm(np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0]), axis=1)
        keep = np.argsort(area)[-MAX_TRIS:]
        T, C = T[keep], C[keep]
    # camera: yaw -45°, pitch 35°, looking at the centre
    yaw, pitch = np.radians(-45), np.radians(35)
    eye_dir = np.array([np.cos(pitch) * np.cos(yaw), np.cos(pitch) * np.sin(yaw), np.sin(pitch)])
    right = np.array([-np.sin(yaw), np.cos(yaw), 0.0])
    up = np.cross(eye_dir, right)
    centre = (T.reshape(-1, 3).min(axis=0) + T.reshape(-1, 3).max(axis=0)) / 2
    P = T - centre
    x, y, depth = P @ right, P @ up, P @ eye_dir
    span = max(x.max() - x.min(), (y.max() - y.min()) * W / H, 1e-9)
    s = 0.86 * W / span
    sx = W / 2 + (x - (x.max() + x.min()) / 2) * s
    sy = H / 2 - (y - (y.max() + y.min()) / 2) * s
    n = np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0])
    n /= np.linalg.norm(n, axis=1, keepdims=True) + 1e-12
    n[(n @ eye_dir) < 0] *= -1  # light both sides
    light = np.array([0.35, -0.25, 0.9])
    light /= np.linalg.norm(light)
    shade = 0.35 + 0.55 * np.clip(n @ light, 0, 1) + 0.15 * np.clip(n @ eye_dir, 0, 1)
    rgb = np.clip(C * shade[:, None] * 255, 0, 255).astype(np.uint8)
    # depth-buffer rasteriser: larger depth = nearer the eye
    frame = np.array(img, dtype=np.uint8)
    zbuf = np.full((H, W), -np.inf)
    for i in range(len(T)):
        x0, x1, x2 = sx[i]
        y0, y1, y2 = sy[i]
        minx, maxx = int(max(np.floor(min(x0, x1, x2)), 0)), int(min(np.ceil(max(x0, x1, x2)), W - 1))
        miny, maxy = int(max(np.floor(min(y0, y1, y2)), 0)), int(min(np.ceil(max(y0, y1, y2)), H - 1))
        if minx > maxx or miny > maxy:
            continue
        area = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
        if abs(area) < 1e-9:
            continue
        px = np.arange(minx, maxx + 1) + 0.5
        py = (np.arange(miny, maxy + 1) + 0.5)[:, None]
        w0 = ((x1 - px) * (y2 - py) - (x2 - px) * (y1 - py)) / area
        w1 = ((x2 - px) * (y0 - py) - (x0 - px) * (y2 - py)) / area
        w2 = 1.0 - w0 - w1
        inside = (w0 >= -1e-6) & (w1 >= -1e-6) & (w2 >= -1e-6)
        if not inside.any():
            continue
        d0, d1, d2 = depth[i]
        z = w0 * d0 + w1 * d1 + w2 * d2
        region = zbuf[miny:maxy + 1, minx:maxx + 1]
        nearer = inside & (z > region)
        region[nearer] = z[nearer]
        frame[miny:maxy + 1, minx:maxx + 1][nearer] = rgb[i]
    return _png(Image.fromarray(frame), size)


def _png(img, size):
    img = img.resize(size, Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
