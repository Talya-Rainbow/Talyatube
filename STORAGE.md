# Keep videos forever (Talya-Tube)

Videos and accounts need **two** kinds of storage:

1. **Database** — titles, users, comments → SQLite file `data/talyatube.sqlite3`
2. **Video files** — the actual `.mp4` bytes → `uploads/` folder **or** Cloudflare R2

On free Render, the server disk can wipe on redeploy. Use one of these:

---

## Option A — Render Persistent Disk (simplest)

1. Render dashboard → your web service → **Disks**
2. Add disk, mount path: `/var/data`
3. Environment variables:

```text
DATA_DIR=/var/data/data
UPLOAD_DIR=/var/data/uploads
```

4. Redeploy

SQLite + uploads then live on the disk and survive restarts.

(Note: disks may require a paid Render plan.)

---

## Option B — Free Cloudflare R2 (videos in the cloud)

1. Create a free account at https://dash.cloudflare.com
2. **R2** → Create bucket (e.g. `talyatube-videos`)
3. Enable **Public access** (or a custom domain) and copy the public URL
4. **Manage R2 API Tokens** → create token with Object Read & Write
5. On Render → Environment:

```text
R2_ACCOUNT_ID=your_account_id
R2_ACCESS_KEY_ID=your_access_key
R2_SECRET_ACCESS_KEY=your_secret
R2_BUCKET=talyatube-videos
R2_PUBLIC_BASE=https://pub-xxxxx.r2.dev
```

6. Redeploy

New uploads go to R2 and stay even if Render rebuilds.

Still set `DATA_DIR` on a disk if you can, so the SQLite database (who uploaded what) also persists.

---

## Local dev

No env vars needed. Uses `./data/talyatube.sqlite3` and `./uploads/`.
