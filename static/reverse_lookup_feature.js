(function () {
  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll("\"", "&quot;")
      .replaceAll("'", "&#39;");
  }

  function createReverseLookupFeature(options) {
    const config = options && options.uiConfig ? options.uiConfig : {};
    const fieldStyles = config.field_styles || {};
    const tagStyles = config.tag_styles || {};
    const storage = options.storage || window.localStorage;
    const prefix = options.prefix || "reverse";
    const rawKey = options.rawStorageKey || (prefix + "-show-raw");
    const requestUrl = options.requestUrl || "/reverse-search";

    const panel = document.getElementById(prefix + "Panel");
    const status = document.getElementById(prefix + "Status");
    const summary = document.getElementById(prefix + "Summary");
    const content = document.getElementById(prefix + "Content");
    const rawToggle = document.getElementById(prefix + "RawToggle");
    const refresh = document.getElementById(prefix + "Refresh");

    if (!panel || !status || !summary || !content || !rawToggle || !refresh) {
      return null;
    }

    let currentItem = null;
    let currentPayload = null;
    let showRaw = storage.getItem(rawKey);
    showRaw = showRaw === null ? !!config.show_raw_data_by_default : showRaw === "1";

    function chipStyle(styleKey) {
      const style = fieldStyles[styleKey] || {};
      const parts = [];
      if (style.background) parts.push("background:" + style.background);
      if (style.border) parts.push("border-color:" + style.border);
      if (style.text) parts.push("color:" + style.text);
      return parts.length ? ' style="' + parts.join(";") + '"' : "";
    }

    function renderLink(label, href) {
      if (!href) return "";
      return '<a class="reverse-link" href="' + escapeHtml(href) + '" target="_blank" rel="noreferrer">' + escapeHtml(label) + "</a>";
    }

    function renderMetricTag(label, value, styleKey, fallbackClass) {
      const cssClass = fallbackClass || "meta-tag-kind";
      if (value === null || value === undefined || value === "") return "";
      return '<span class="meta-tag ' + cssClass + '"' + chipStyle(styleKey) + '><span class="meta-tag-label">' + escapeHtml(label) + '</span><span class="meta-tag-value">' + escapeHtml(value) + "</span></span>";
    }

    function findTagStyle(tag, groupLabel) {
      const lowered = String(tag || "").toLowerCase();
      const grouped = tagStyles[groupLabel];
      if (grouped) return grouped;
      for (const [name, style] of Object.entries(tagStyles)) {
        const keywords = Array.isArray(style.keywords) ? style.keywords : [];
        if (keywords.some((keyword) => lowered.includes(String(keyword).toLowerCase()))) {
          return style;
        }
      }
      return fieldStyles[groupLabel] || {};
    }

    function renderTagPill(tag, groupLabel) {
      const style = findTagStyle(tag, groupLabel);
      const css = [];
      if (style.background) css.push("background:" + style.background);
      if (style.border) css.push("border-color:" + style.border);
      if (style.text) css.push("color:" + style.text);
      return '<span class="reverse-tag"' + (css.length ? ' style="' + css.join(";") + '"' : "") + ">" + escapeHtml(tag) + "</span>";
    }

    function tag(label, value, cls) {
      if (!value) return "";
      return '<span class="meta-tag ' + cls + '"><span class="meta-tag-label">' + escapeHtml(label) + '</span><span class="meta-tag-value">' + escapeHtml(value) + "</span></span>";
    }

    function renderReverseSummary(resource) {
      if (!resource || typeof resource !== "object") {
        return '<div class="reverse-empty">No lookup data yet.</div>';
      }
      const summaryData = resource.summary || {};
      const sections = [];

      const metrics = [
        tag("post", summaryData.post_id, "meta-tag-name"),
        renderMetricTag("match", summaryData.match_score, "match_score"),
        tag("score", summaryData.score, "meta-tag-kind"),
        renderMetricTag("up", summaryData.up_score, "up_score"),
        renderMetricTag("down", summaryData.down_score, "down_score"),
        renderMetricTag("rating", summaryData.rating, "rating"),
        tag("favs", summaryData.fav_count, "meta-tag-kind")
      ].join("");

      if (metrics) {
        sections.push('<div class="reverse-section"><div class="reverse-label">Signals</div><div class="meta-tags">' + metrics + "</div></div>");
      }

      const links = [
        renderLink("Post", summaryData.post_url),
        renderLink("Source", summaryData.source_url),
        renderLink("File", summaryData.file_url),
        renderLink("Sample", summaryData.sample_url)
      ].filter(Boolean).join(" · ");

      if (links) {
        sections.push('<div class="reverse-section"><div class="reverse-label">Links</div><div>' + links + "</div></div>");
      }

      const details = [
        tag("created", summaryData.created_at, "meta-tag-date"),
        tag("updated", summaryData.updated_at, "meta-tag-date"),
        tag("res", summaryData.width && summaryData.height ? (summaryData.width + "x" + summaryData.height) : "", "meta-tag-res"),
        tag("ext", summaryData.file_ext, "meta-tag-kind"),
        tag("tags", summaryData.tag_count, "meta-tag-kind")
      ].join("");

      if (details) {
        sections.push('<div class="reverse-section"><div class="reverse-label">Details</div><div class="meta-tags">' + details + "</div></div>");
      }

      const tagGroups = Array.isArray(summaryData.tag_groups) ? summaryData.tag_groups : [];
      if (tagGroups.length) {
        const groupsHtml = tagGroups.map((group) => {
          const pills = (group.tags || []).slice(0, 40).map((tagValue) => renderTagPill(tagValue, group.label)).join("");
          return '<div class="reverse-group"><div class="reverse-label">' + escapeHtml(group.label) + '</div><div class="reverse-tags">' + (pills || '<span class="reverse-empty">No tags</span>') + "</div></div>";
        }).join("");
        sections.push('<div class="reverse-section"><div class="reverse-label">Tags</div>' + groupsHtml + "</div>");
      }

      if (!sections.length) {
        return '<div class="reverse-empty">No parsed fields available yet.</div>';
      }
      return sections.join("");
    }

    function applyRawState() {
      content.style.display = showRaw ? "block" : "none";
      summary.style.display = showRaw ? "none" : "grid";
      rawToggle.textContent = showRaw ? "Hide raw" : "Show raw";
      rawToggle.style.display = config.show_raw_toggle === false ? "none" : "inline-flex";
      storage.setItem(rawKey, showRaw ? "1" : "0");
    }

    function setState(item, statusText, payload, disabled) {
      const lookupPayload = payload && payload.data ? payload.data : payload;
      currentPayload = payload || null;
      currentItem = item || currentItem;
      panel.style.display = "block";
      status.textContent = statusText || "";
      summary.innerHTML = renderReverseSummary(lookupPayload);
      content.textContent = payload ? JSON.stringify(lookupPayload, null, 2) : "";
      refresh.disabled = !!disabled;
      applyRawState();
    }

    async function load(item, force) {
      if (!item || !item.path) {
        panel.style.display = "none";
        return null;
      }
      currentItem = item;
      if (!force && item.reverseSearch) {
        setState(item, item.reverseSearch.cached ? "Loaded from data.json" : "Loaded from live result", item.reverseSearch, false);
        return item.reverseSearch;
      }

      setState(item, force ? "Refreshing..." : "Loading...", null, true);
      try {
        const res = await fetch(requestUrl, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ rel_path: item.path, force: !!force })
        });
        const payload = await res.json().catch(() => ({}));
        if (!res.ok || !payload.ok) {
          setState(item, payload.message || "Lookup failed", payload, false);
          return payload;
        }
        item.reverseSearch = payload;
        if (currentItem === item) {
          setState(item, payload.cached ? "Loaded from data.json" : "Updated from e621", payload, false);
        }
        return payload;
      } catch (_) {
        const payload = { error: "Request failed" };
        setState(item, "Lookup failed", payload, false);
        return payload;
      }
    }

    rawToggle.addEventListener("click", () => {
      showRaw = !showRaw;
      applyRawState();
    });

    refresh.addEventListener("click", () => {
      if (!currentItem) return;
      load(currentItem, true);
    });

    applyRawState();

    return {
      load,
      setState,
      clear() {
        currentItem = null;
        currentPayload = null;
        panel.style.display = "none";
      },
      getCurrentItem() {
        return currentItem;
      },
      getCurrentPayload() {
        return currentPayload;
      }
    };
  }

  window.createReverseLookupFeature = createReverseLookupFeature;
})();
