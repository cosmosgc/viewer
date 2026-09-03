/* Shared remote-post viewer component.
   Canonical behaviour extracted from the Watch view so Watch, Lookup
   (and Pinned's modal shell) share identical card actions, modal
   controls and keybinds:
     A/D or Left/Right = prev/next, H = hide data, I = import,
     O = open file, P = open post, Space = play/pause, Esc = close.
   Usage:
     const viewer = window.createRemotePostViewer({ prefix: "watch", importUrl });
     viewer.setPosts(posts);
     grid.innerHTML = viewer.renderCards({ showTags: true });
     viewer.openAt(postId);
     viewer.importPost(postId, buttonEl?) / viewer.importCurrent(buttonEl?)
*/
(() => {
  const escapeHtml = (value) => String(value ?? "")
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll("\"", "&quot;").replaceAll("'", "&#39;");

  const formatDate = (value) => value ? String(value).replace("T", " ").replace("Z", " UTC") : "";
  const formatDir = (value) => value ? String(value).replaceAll("\\", "/") : "";
  const formatBytes = (bytes) => {
    const numeric = Number(bytes);
    if (!Number.isFinite(numeric) || numeric <= 0) return "";
    const units = ["B", "KB", "MB", "GB", "TB"];
    const idx = Math.min(Math.floor(Math.log(numeric) / Math.log(1024)), units.length - 1);
    const value = numeric / Math.pow(1024, idx);
    return value.toFixed(value >= 10 || idx === 0 ? 0 : 1) + " " + units[idx];
  };

  const getPostFileExt = (post) => String((post && post.file && post.file.ext) || "").toLowerCase();

  const classifyResourceType = (ext) => {
    if (!ext) return { kind: "file", label: "File" };
    if (ext === "gif") return { kind: "gif", label: "GIF" };
    if (["swf"].includes(ext)) return { kind: "flash", label: "Flash" };
    if (["webm", "mp4", "mov", "mkv"].includes(ext)) return { kind: "video", label: "Video" };
    if (["jpg", "jpeg", "png", "webp", "bmp", "avif", "jxl"].includes(ext)) return { kind: "image", label: "Image" };
    return { kind: "file", label: ext.toUpperCase() };
  };

  const postFileUrl = (post) => (post && post.file && post.file.url) || "";
  const postPreviewUrl = (post) => {
    if (!post) return "";
    if (post.preview && post.preview.url) return post.preview.url;
    if (post.sample && post.sample.url) return post.sample.url;
    return "";
  };
  const postPageUrl = (post) => (post && post.id ? `https://e621.net/posts/${post.id}` : "");

  const importedStateMarkup = (post) => {
    const state = (post && post.library_state) || {};
    if (state.imported) return '<span class="rpv-state-pill rpv-imported">Imported</span>';
    if (state.target_dir) return '<span class="rpv-state-pill rpv-pending">Not Imported</span>';
    return '<span class="rpv-state-pill rpv-neutral">Remote</span>';
  };

  const importedDetailsMarkup = (post) => {
    const state = (post && post.library_state) || {};
    if (state.imported && Array.isArray(state.matching_files) && state.matching_files.length) {
      return `<div class="rpv-result-card-text">Library files: ${escapeHtml(state.matching_files.slice(0, 2).join(", "))}</div>`;
    }
    if (state.target_dir) {
      return `<div class="rpv-result-card-text">Target dir: ${escapeHtml(formatDir(state.target_dir))}</div>`;
    }
    return "";
  };

  const tagPillsMarkup = (post, { showTags = true, maxTags = 8 } = {}) => {
    const tags = (((post && post.tags) || {}).general) || [];
    const sliced = Array.isArray(tags) ? tags.slice(0, maxTags) : [];
    if (!sliced.length) return "";
    return `<div class="rpv-tag-row${showTags ? "" : " rpv-hidden"}">${sliced.map((t) => `<span class="rpv-tag-pill">${escapeHtml(t)}</span>`).join("")}</div>`;
  };

  const renderPostCard = (post, { showTags = true } = {}) => {
    if (!post) return "";
    const previewUrl = postPreviewUrl(post);
    const fileUrl = postFileUrl(post);
    const postUrl = postPageUrl(post);
    const type = classifyResourceType(getPostFileExt(post));
    const imported = Boolean(post.library_state && post.library_state.imported);
    const importLabel = imported ? "Import Again" : "Import";
    const generalTags = (((post.tags || {}).general) || []).slice(0, 8);
    return `<article class="rpv-result-card rpv-type-${type.kind}${imported ? " rpv-imported" : ""}" data-post-card-id="${escapeHtml(post.id)}">`
      + (previewUrl
        ? `<button type="button" class="rpv-preview-wrap" data-action="rpv-open" data-post-id="${escapeHtml(post.id)}"><img src="${escapeHtml(previewUrl)}" alt="${escapeHtml(post.id)}" loading="lazy"></button>`
        : `<div class="rpv-preview-wrap"></div>`)
      + `<div class="rpv-result-card-body">`
      + `<div class="rpv-result-card-title">Post #${escapeHtml(post.id)} <span class="rpv-type-pill">${escapeHtml(type.label)}</span> ${importedStateMarkup(post)}</div>`
      + `<div class="rpv-result-card-text">Created: ${escapeHtml(formatDate(post.created_at))}</div>`
      + `<div class="rpv-result-card-text">Rating: ${escapeHtml(post.rating || "")} &middot; Score: ${escapeHtml(post.score && post.score.total)} &middot; Favs: ${escapeHtml(post.fav_count)}</div>`
      + importedDetailsMarkup(post)
      + `<div class="rpv-card-actions">`
      + `<button type="button" class="rpv-card-btn" data-action="rpv-open" data-post-id="${escapeHtml(post.id)}">Open</button>`
      + (postUrl ? `<a class="rpv-card-btn rpv-secondary" href="${escapeHtml(postUrl)}" target="_blank" rel="noreferrer">Open Post</a>` : "")
      + ((fileUrl || previewUrl) ? `<a class="rpv-card-btn rpv-secondary" href="${escapeHtml(fileUrl || previewUrl)}" target="_blank" rel="noreferrer">Open File</a>` : "")
      + `<button type="button" class="rpv-card-btn rpv-secondary" data-action="rpv-import" data-post-id="${escapeHtml(post.id)}">${escapeHtml(importLabel)}</button>`
      + `</div>`
      + (generalTags.length ? `<div class="rpv-tag-row${showTags ? "" : " rpv-hidden"}">${generalTags.map((t) => `<span class="rpv-tag-pill">${escapeHtml(t)}</span>`).join("")}</div>` : "")
      + `</div></article>`;
  };

  const renderModalMeta = (post) => {
    const tags = (post && post.tags) || {};
    const groups = ["artist", "copyright", "character", "species", "general", "meta", "lore"];
    const tagGroups = groups
      .filter((g) => Array.isArray(tags[g]) && tags[g].length)
      .map((g) => `<div class="rpv-modal-meta-group"><div class="rpv-modal-meta-label">${escapeHtml(g)}</div><div class="rpv-modal-tag-list">${tags[g].slice(0, g === "general" ? 24 : 10).map((t) => `<span class="rpv-modal-tag">${escapeHtml(t)}</span>`).join("")}</div></div>`)
      .join("");
    const state = (post && post.library_state) || {};
    const file = (post && post.file) || {};
    return `<div class="rpv-modal-meta-group"><div class="rpv-modal-meta-label">Post</div>`
      + `<div>Post #${escapeHtml(post.id)}${post.id ? ` &middot; <a href="https://e621.net/posts/${escapeHtml(post.id)}" target="_blank" rel="noreferrer">open</a>` : ""}</div>`
      + `<div>Created: ${escapeHtml(formatDate(post.created_at))}</div>`
      + `<div>Uploader: ${escapeHtml(post.uploader_name || post.uploader_id || "")}</div>`
      + `<div>Rating: ${escapeHtml(post.rating || "")} &middot; Score: ${escapeHtml(post.score && post.score.total)} &middot; Favs: ${escapeHtml(post.fav_count)}</div>`
      + `<div>File: ${escapeHtml(file.ext || "")} ${escapeHtml(file.width ? `${file.width}x${file.height}` : "")} ${escapeHtml(formatBytes(file.size))}</div>`
      + `<div>Type: ${escapeHtml(classifyResourceType(getPostFileExt(post)).label)}</div>`
      + `<div>Library: ${state.imported ? "Imported" : "Not imported yet"}</div>`
      + (state.target_dir ? `<div>Target dir: ${escapeHtml(formatDir(state.target_dir))}</div>` : "")
      + (state.imported && Array.isArray(state.matching_files) && state.matching_files.length ? `<div>Files: ${escapeHtml(state.matching_files.join(", "))}</div>` : "")
      + `</div>`
      + (post.description ? `<div class="rpv-modal-meta-group"><div class="rpv-modal-meta-label">Description</div><div>${escapeHtml(post.description)}</div></div>` : "")
      + tagGroups;
  };

  window.RemotePostViewerUtils = {
    escapeHtml, formatDate, formatDir, formatBytes,
    getPostFileExt, classifyResourceType,
    postFileUrl, postPreviewUrl, postPageUrl,
    renderPostCard, renderModalMeta, tagPillsMarkup,
  };

  window.createRemotePostViewer = ({ prefix = "rpv", importUrl = "", onStatus = null, onPostsChanged = null } = {}) => {
    const $ = (id) => document.getElementById(id);
    const modal = $(`${prefix}Modal`);
    if (!modal) throw new Error(`RemotePostViewer: missing modal #${prefix}Modal`);
    const shell = modal.querySelector(".rpv-modal-shell");
    const counter = $(`${prefix}Counter`);
    const image = $(`${prefix}Image`);
    const video = $(`${prefix}Video`);
    const fallback = $(`${prefix}Fallback`);
    const meta = $(`${prefix}Meta`);
    const sidebarToggle = $(`${prefix}SidebarToggle`);
    const importBtn = $(`${prefix}Import`);
    const openFileBtn = $(`${prefix}OpenFile`);
    const openPostBtn = $(`${prefix}OpenPost`);
    const prevBtn = $(`${prefix}Prev`);
    const nextBtn = $(`${prefix}Next`);
    const closeBtn = $(`${prefix}Close`);
    const toast = $(`${prefix}Toast`);

    let posts = [];
    let index = 0;
    let open = false;
    let sidebarVisible = true;

    const emitStatus = (message, tone = "") => { if (typeof onStatus === "function") onStatus(message, tone); };
    const emitChanged = () => { if (typeof onPostsChanged === "function") onPostsChanged(posts); };

    const current = () => posts[index] || null;

    const showToast = (message) => {
      if (!toast) return;
      toast.textContent = message;
      toast.style.display = "block";
      window.setTimeout(() => { toast.style.display = "none"; }, 1800);
    };

    const applySidebarState = () => {
      shell.classList.toggle("rpv-sidebar-hidden", !sidebarVisible);
      if (sidebarToggle) sidebarToggle.textContent = sidebarVisible ? "Hide Data" : "Show Data";
    };

    const renderModal = () => {
      const post = current();
      if (!post) return;
      if (counter) counter.textContent = `${index + 1} / ${posts.length}`;
      if (meta) meta.innerHTML = renderModalMeta(post);
      if (importBtn) importBtn.textContent = post.library_state && post.library_state.imported ? "Import Again" : "Import To Library";
      const fileUrl = postFileUrl(post);
      const type = classifyResourceType(getPostFileExt(post));
      if (fallback) { fallback.style.display = "none"; fallback.innerHTML = ""; }
      if (type.kind === "video" && fileUrl) {
        image.style.display = "none";
        image.removeAttribute("src");
        video.src = fileUrl;
        video.style.display = "block";
        video.load();
      } else if (["image", "gif"].includes(type.kind)) {
        video.pause();
        video.removeAttribute("src");
        video.style.display = "none";
        image.src = fileUrl || postPreviewUrl(post);
        image.style.display = "block";
      } else {
        video.pause();
        video.removeAttribute("src");
        video.style.display = "none";
        image.style.display = "none";
        image.removeAttribute("src");
        if (fallback) {
          fallback.innerHTML = fileUrl
            ? `Preview unavailable for ${escapeHtml(type.label)} files. <a href="${escapeHtml(fileUrl)}" target="_blank" rel="noreferrer">Open the original file</a>.`
            : `Preview unavailable for ${escapeHtml(type.label)} files.`;
          fallback.style.display = "block";
        }
      }
    };

    const openAt = (postId) => {
      const found = posts.findIndex((p) => String(p.id) === String(postId));
      if (found < 0) return false;
      index = found;
      open = true;
      modal.classList.add("open");
      modal.setAttribute("aria-hidden", "false");
      applySidebarState();
      renderModal();
      return true;
    };

    const close = () => {
      open = false;
      modal.classList.remove("open");
      modal.setAttribute("aria-hidden", "true");
      video.pause();
      video.removeAttribute("src");
      if (toast) toast.style.display = "none";
    };

    const prev = () => { if (posts.length) { index = (index - 1 + posts.length) % posts.length; renderModal(); } };
    const next = () => { if (posts.length) { index = (index + 1) % posts.length; renderModal(); } };
    const openFile = () => { const post = current(); const url = post && postFileUrl(post); if (url) window.open(url, "_blank", "noopener,noreferrer"); };
    const openPost = () => { const post = current(); if (post && post.id) window.open(`https://e621.net/posts/${post.id}`, "_blank", "noopener,noreferrer"); };

    const setImportBusy = (busy, button, idleLabel) => {
      if (button) {
        button.disabled = busy;
        if (busy) button.textContent = "Importing...";
        else if (idleLabel) button.textContent = idleLabel;
      }
      if (importBtn) {
        importBtn.disabled = busy;
        if (busy) importBtn.textContent = "Importing...";
      }
    };

    const markPostImported = (post, payload) => {
      if (!post) return;
      const relPath = String((payload && payload.rel_path) || "");
      const fileName = relPath ? relPath.split(/[\\/]/).pop() : "";
      const targetDir = relPath ? relPath.split(/[\\/]/).slice(0, -1).join("/") : (((post.library_state) || {}).target_dir || "");
      const nextFiles = Array.isArray(post.library_state && post.library_state.matching_files) ? [...post.library_state.matching_files] : [];
      if (fileName && !nextFiles.includes(fileName)) nextFiles.unshift(fileName);
      post.library_state = { ...((post.library_state) || {}), imported: true, target_dir: targetDir, matching_files: nextFiles };
    };

    const importPost = async (postId, button = null) => {
      const post = posts.find((p) => String(p.id) === String(postId)) || current();
      if (!post || !importUrl) return false;
      const wasImported = Boolean(post.library_state && post.library_state.imported);
      const idleLabel = wasImported ? "Import Again" : "Import";
      setImportBusy(true, button);
      if (importBtn) { importBtn.disabled = true; importBtn.textContent = "Importing..."; }
      emitStatus(`Importing post #${post.id}...`, "warn");
      try {
        const response = await fetch(importUrl, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ post }) });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok || !payload.ok) {
          emitStatus(payload.message || "Import failed", "error");
          showToast(payload.message || "Import failed");
          return false;
        }
        markPostImported(post, payload);
        emitChanged();
        if (open) renderModal();
        emitStatus(payload.message || "Imported", "ok");
        showToast(payload.message || "Imported");
        return true;
      } catch (_) {
        emitStatus("Import failed", "error");
        showToast("Import failed");
        return false;
      } finally {
        setImportBusy(false, button, post.library_state && post.library_state.imported ? "Import Again" : idleLabel);
        if (importBtn) { importBtn.disabled = false; }
        if (open) renderModal();
      }
    };

    const importCurrent = (button = null) => {
      const post = current();
      return post ? importPost(post.id, button) : Promise.resolve(false);
    };

    // Shared click delegation for cards rendered by renderCards().
    const bindGrid = (gridEl) => {
      if (!gridEl || gridEl.dataset.rpvBound) return;
      gridEl.dataset.rpvBound = "1";
      gridEl.addEventListener("click", (event) => {
        const trigger = event.target.closest('[data-action="rpv-open"], [data-action="rpv-import"]');
        if (!trigger || !gridEl.contains(trigger)) return;
        const postId = trigger.dataset.postId;
        if (trigger.dataset.action === "rpv-open") openAt(postId);
        else if (trigger.dataset.action === "rpv-import") importPost(postId, trigger);
      });
    };

    modal.addEventListener("click", (event) => { if (event.target === modal) close(); });
    if (closeBtn) closeBtn.addEventListener("click", close);
    if (prevBtn) prevBtn.addEventListener("click", prev);
    if (nextBtn) nextBtn.addEventListener("click", next);
    if (sidebarToggle) sidebarToggle.addEventListener("click", () => { sidebarVisible = !sidebarVisible; applySidebarState(); });
    if (openFileBtn) openFileBtn.addEventListener("click", openFile);
    if (openPostBtn) openPostBtn.addEventListener("click", openPost);
    if (importBtn) importBtn.addEventListener("click", () => importCurrent());

    const keyHandler = (event) => {
      if (!open) return;
      const t = event.target;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
      const key = event.key.toLowerCase();
      if (key === "escape") { event.preventDefault(); close(); return; }
      if (key === "arrowleft" || key === "a") { event.preventDefault(); prev(); return; }
      if (key === "arrowright" || key === "d") { event.preventDefault(); next(); return; }
      if (key === "h") { event.preventDefault(); sidebarVisible = !sidebarVisible; applySidebarState(); return; }
      if (key === "i" && importBtn) { event.preventDefault(); importCurrent(); return; }
      if (key === "o") { event.preventDefault(); openFile(); return; }
      if (key === "p") { event.preventDefault(); openPost(); return; }
      if (key === " " && video.style.display !== "none") {
        event.preventDefault();
        if (video.paused) video.play().catch(() => {});
        else video.pause();
      }
    };
    window.addEventListener("keydown", keyHandler);

    return {
      prefix,
      get posts() { return posts; },
      get isOpen() { return open; },
      current,
      setPosts(nextPosts) { posts = Array.isArray(nextPosts) ? nextPosts : []; index = 0; },
      renderCards: ({ showTags = true } = {}) => {
        if (!posts.length) return '<div class="rpv-empty-state">No posts found.</div>';
        return posts.map((p) => renderPostCard(p, { showTags })).join("");
      },
      bindGrid,
      openAt, close, prev, next, openFile, openPost,
      importPost, importCurrent, markPostImported, renderModal,
    };
  };
})();
