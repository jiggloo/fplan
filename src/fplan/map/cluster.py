"""Pure post-processing of a raw map-extract dump.

Everything here operates on the in-memory dict the extract mod produces — no
Factorio, no subprocess, no I/O — so it is fully unit-testable against a
captured fixture. The k-means / silhouette routines are carried over verbatim
from the upstream ``l3_map.py`` (the verified canonical-seed implementation);
they are deterministic so a given dump always yields the same clustering.
"""

from __future__ import annotations

import numpy as np

# Factorio 1.1 crude-oil: the resource `amount` divided by this is the pumpjack
# yield percentage shown in-game (300000 -> 100%, 60000 floor -> 20%).
OIL_YIELD_PER_PCT = 3000.0


def _kmeans(points: np.ndarray, k: int, iters: int = 100):
    """Deterministic k-means: farthest-first (Gonzalez) init seeded from the
    first point, then Lloyd's iterations. No RNG, so the same input always
    yields the same clustering — required for reproducible canonical-seed runs.
    Returns (labels, centers)."""
    n = len(points)
    k = max(1, min(k, n))
    chosen = [0]
    if k > 1:
        d = np.linalg.norm(points - points[0], axis=1)
        for _ in range(1, k):
            nxt = int(np.argmax(d))
            chosen.append(nxt)
            d = np.minimum(d, np.linalg.norm(points - points[nxt], axis=1))
    centers = points[chosen].astype(float)
    labels = np.full(n, -1)
    for it in range(iters):
        dists = np.linalg.norm(points[:, None, :] - centers[None, :, :], axis=2)
        new_labels = np.argmin(dists, axis=1)
        if it > 0 and np.array_equal(new_labels, labels):
            break
        labels = new_labels
        for j in range(k):
            mask = labels == j
            if mask.any():
                centers[j] = points[mask].mean(axis=0)
    return labels, centers


def _silhouette(points: np.ndarray, labels: np.ndarray) -> float:
    """Mean silhouette coefficient (in [-1, 1]); higher is a cleaner partition.
    Used only to auto-pick k, so the O(n^2) distance matrix is fine for the
    handful of oil spots a map ever has."""
    uniq = np.unique(labels)
    if len(uniq) < 2:
        return -1.0
    dmat = np.linalg.norm(points[:, None, :] - points[None, :, :], axis=2)
    sil = np.zeros(len(points))
    for i in range(len(points)):
        same = labels == labels[i]
        same[i] = False
        a = dmat[i, same].mean() if same.any() else 0.0
        b = min(dmat[i, labels == j].mean() for j in uniq if j != labels[i])
        sil[i] = 0.0 if max(a, b) == 0 else (b - a) / max(a, b)
    return float(sil.mean())


def cluster_oil(oil_spots: list[dict], k="auto", kmax: int = 10) -> list[dict]:
    """k-means over oil-spot positions -> oil fields. Mutates each spot dict
    with a `cluster` id and returns the cluster list (centroid, spot_count,
    total_amount, total_yield_pct), shaped to mirror resource patches. `k`
    is an int, or "auto" to pick the best silhouette over k in [2, kmax]."""
    if not oil_spots:
        return []
    pts = np.array([[s["x"], s["y"]] for s in oil_spots], dtype=float)
    n = len(pts)

    if isinstance(k, int):
        kk = max(1, min(k, n))
    elif n <= 2:
        kk = n
    else:
        best_k, best_s = 2, -2.0
        for cand in range(2, min(kmax, n - 1) + 1):
            labels, _ = _kmeans(pts, cand)
            score = _silhouette(pts, labels)
            if score > best_s:
                best_s, best_k = score, cand
        kk = best_k

    labels, centers = _kmeans(pts, kk)

    # Relabel clusters nearest-first so ids are stable and meaningful.
    raw = sorted(np.unique(labels), key=lambda j: float(np.hypot(*centers[j])))
    remap = {old: new for new, old in enumerate(raw)}

    clusters: list[dict] = []
    for old in raw:
        idx = np.where(labels == old)[0]
        cx, cy = (float(v) for v in centers[old])
        total = sum(oil_spots[i]["amount"] for i in idx)
        clusters.append(
            {
                "id": remap[old],
                "centroid_x": cx,
                "centroid_y": cy,
                "spot_count": int(len(idx)),
                "total_amount": int(total),
                "total_yield_pct": round(total / OIL_YIELD_PER_PCT, 1),
                "distance": float(np.hypot(cx, cy)),
            }
        )
    for i, lab in enumerate(labels):
        oil_spots[i]["cluster"] = remap[int(lab)]
    return clusters


def postprocess(data: dict, oil_k="auto") -> dict:
    """Finalize a raw dump for output: sort patches/spots by distance, then
    k-means the oil spots into fields (assigning each spot a `cluster` id)."""
    if "patches" in data:
        data["patches"] = sorted(data["patches"], key=lambda p: p["distance"])
    if "water_patches" in data:
        data["water_patches"] = sorted(
            data["water_patches"], key=lambda p: p["distance"]
        )
    if "oil_spots" in data:
        data["oil_spots"] = sorted(data["oil_spots"], key=lambda s: s["distance"])
        data["oil_clusters"] = cluster_oil(data["oil_spots"], k=oil_k)
    return data
