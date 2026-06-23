// ==UserScript==
// @name         Chatrove Extractor
// @namespace    gemini-vault
// @version      1.0.0
// @description  Full export of all chats and Canvas artifacts from Google Gemini
// @author       Chatrove
// @match        https://gemini.google.com/*
// @grant        none
// @run-at       document-idle
// ==/UserScript==

(function () {
  "use strict";

  // ==================== CONFIG ====================
  const CONFIG = {
    DELAY_BETWEEN_REQUESTS_MS: 1500,
    MAX_RETRIES: 3,
    RETRY_BACKOFF_MS: 3000,
    MAX_CONVERSATIONS_PER_BATCH: 20,
    EXPORT_FILENAME_PREFIX: "gemini_vault_export",
    VERSION: "1.0.0",
  };

  // ==================== STATE ====================
  const state = {
    conversations: new Map(),
    csrfToken: null,
    buildLabel: null,
    accountEmail: null,
    isExtracting: false,
    cancelRequested: false,
    stats: { total: 0, downloaded: 0, errors: 0, canvasItems: 0 },
  };

  // ==================== HELPERS ====================

  function sleep(ms) {
    return new Promise((r) => setTimeout(r, ms));
  }

  function extractPageTokens() {
    // CSRF token (at) — look in several places
    let at = null;
    let bl = null;

    // Method 1: WIZ_global_data
    if (typeof WIZ_global_data !== "undefined") {
      at = WIZ_global_data.SNlM0e;
      bl = WIZ_global_data.cfb2h;
    }

    // Method 2: search in page scripts
    if (!at) {
      const scripts = document.querySelectorAll("script");
      for (const s of scripts) {
        const text = s.textContent || "";
        const atMatch = text.match(/SNlM0e":"([^"]+)"/);
        if (atMatch) at = atMatch[1];
        const blMatch = text.match(/cfb2h":"([^"]+)"/);
        if (blMatch) bl = blMatch[1];
        if (at && bl) break;
      }
    }

    // Method 3: meta tags
    if (!at) {
      const metaAt = document.querySelector('meta[name="at"]');
      if (metaAt) at = metaAt.content;
    }

    // Detect the account email
    let email = null;
    try {
      const scripts = document.querySelectorAll("script");
      for (const s of scripts) {
        const text = s.textContent || "";
        const emailMatch = text.match(
          /["\s]([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})["\s]/
        );
        if (emailMatch) {
          email = emailMatch[1];
          break;
        }
      }
    } catch (_) {}

    return { at, bl, email };
  }

  function parseBatchResponse(rawText) {
    // Strip the anti-XSRF prefix  )]}'\n
    let text = rawText;
    if (text.startsWith(")]}'")) {
      text = text.substring(text.indexOf("\n") + 1);
    }

    const results = [];

    // The response consists of length-prefixed chunks
    // Format: number (length)\nJSON array
    let pos = 0;
    while (pos < text.length) {
      // Skip whitespace / line breaks
      while (pos < text.length && /\s/.test(text[pos])) pos++;
      if (pos >= text.length) break;

      // Read the chunk length
      let lenStr = "";
      while (pos < text.length && /\d/.test(text[pos])) {
        lenStr += text[pos];
        pos++;
      }
      if (!lenStr) break;

      // Skip the line break after the length
      if (text[pos] === "\n") pos++;

      const chunkLen = parseInt(lenStr, 10);
      if (isNaN(chunkLen) || chunkLen <= 0) break;

      const chunk = text.substring(pos, pos + chunkLen);
      pos += chunkLen;

      try {
        const parsed = JSON.parse(chunk);
        results.push(parsed);
      } catch (_) {
        // Not every chunk is valid JSON; skip it
      }
    }

    return results;
  }

  function deepFindArrays(obj, depth = 0) {
    // Recursive search for arrays with chat data in a nested response
    if (depth > 15 || obj == null) return [];
    if (Array.isArray(obj)) return [obj];
    if (typeof obj === "object") {
      const results = [];
      for (const v of Object.values(obj)) {
        results.push(...deepFindArrays(v, depth + 1));
      }
      return results;
    }
    return [];
  }

  async function batchExecute(rpcId, payload) {
    const { at, bl } = state;
    if (!at) throw new Error("CSRF token (at) not found. Reload the page.");

    const reqData = JSON.stringify([
      [
        [rpcId, JSON.stringify(payload), null, "generic"],
      ],
    ]);

    const params = new URLSearchParams();
    params.set("f.req", reqData);
    params.set("at", at);
    if (bl) params.set("bl", bl);

    const url = "/_/BardChatUi/data/batchexecute?" + new URLSearchParams({
      "rpcids": rpcId,
      "source-path": "/",
      "bl": bl || "",
      "rt": "c",
    }).toString();

    const resp = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
      },
      body: params.toString(),
      credentials: "include",
    });

    if (!resp.ok) {
      throw new Error(`HTTP ${resp.status}: ${resp.statusText}`);
    }

    const text = await resp.text();
    return parseBatchResponse(text);
  }

  // ==================== EXTRACTION METHODS ====================

  function extractConversationsFromDOM() {
    // Method 1: extraction from the DOM (sidebar)
    const convItems = [];

    // Find sidebar elements with chats
    const sidebarItems = document.querySelectorAll(
      'a[href*="/app/"], a[href*="/c/"], [data-conversation-id]'
    );
    for (const el of sidebarItems) {
      const href = el.getAttribute("href") || "";
      const dataId = el.getAttribute("data-conversation-id");

      let convId = dataId;
      if (!convId) {
        // Try to extract the ID from href: /app/<id> or /c/<id>
        const match = href.match(/\/(?:app|c)\/([a-f0-9-]+)/i);
        if (match) convId = match[1];
      }

      if (convId) {
        const title = (el.textContent || "").trim().substring(0, 200);
        convItems.push({ id: convId, title, source: "dom" });
      }
    }

    return convItems;
  }

  function extractConversationsFromPageData() {
    // Method 2: extraction from embedded page data
    const convItems = [];

    const scripts = document.querySelectorAll("script");
    for (const s of scripts) {
      const text = s.textContent || "";
      // Look for arrays with conversation IDs (UUID-like strings)
      const uuidPattern = /["\[]([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})["\]]/gi;
      let match;
      while ((match = uuidPattern.exec(text)) !== null) {
        const id = match[1];
        if (!convItems.some((c) => c.id === id)) {
          convItems.push({ id, title: "", source: "page_data" });
        }
      }
    }

    return convItems;
  }

  async function fetchConversationContent(convId) {
    // Try to fetch the chat content via a direct URL
    const url = `/app/${convId}`;

    try {
      const resp = await fetch(url, {
        credentials: "include",
        headers: {
          Accept: "text/html",
        },
      });

      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}`);
      }

      const html = await resp.text();

      // Extract the chat data from the page HTML
      return parseConversationHTML(html, convId);
    } catch (err) {
      log(`  ⚠ Failed to load ${convId}: ${err.message}`);
      return null;
    }
  }

  function parseConversationHTML(html, convId) {
    const parser = new DOMParser();
    const doc = parser.parseFromString(html, "text/html");

    const conversation = {
      id: convId,
      title: "",
      messages: [],
      canvas_artifacts: [],
      created_time: null,
      updated_time: null,
      raw_html_length: html.length,
    };

    // Extract the title
    const titleEl = doc.querySelector("title");
    if (titleEl) {
      conversation.title = titleEl.textContent.replace(" - Gemini", "").trim();
    }

    // Look for embedded data in scripts
    const scripts = doc.querySelectorAll("script");
    for (const s of scripts) {
      const text = s.textContent || "";

      // Look for JSON arrays with chat data
      // Gemini embeds data via window.WIZ_global_data, AF_initDataCallback, etc.
      const afCallbacks = text.matchAll(
        /AF_initDataCallback\s*\(\s*\{[^}]*data:\s*(\[[\s\S]*?\])\s*\}\s*\)/g
      );
      for (const cb of afCallbacks) {
        try {
          const data = JSON.parse(cb[1]);
          extractMessagesFromData(data, conversation);
        } catch (_) {}
      }

      // Alternative format: data as an array in a script
      if (text.includes('"c_"') || text.includes('"r_"')) {
        try {
          // Look for large JSON blocks
          const jsonBlocks = text.matchAll(
            /(\[\[[\s\S]{100,}?\]\])/g
          );
          for (const jb of jsonBlocks) {
            try {
              const data = JSON.parse(jb[1]);
              extractMessagesFromData(data, conversation);
            } catch (_) {}
          }
        } catch (_) {}
      }
    }

    // If no data was found in scripts — extract from the DOM
    if (conversation.messages.length === 0) {
      extractMessagesFromRenderedDOM(doc, conversation);
    }

    return conversation;
  }

  function extractMessagesFromData(data, conversation) {
    if (!Array.isArray(data)) return;

    // Recursively walk arrays looking for a message pattern
    // Typical structure: [role_marker, content_text, timestamp, ...]
    function walk(arr, depth) {
      if (depth > 10 || !Array.isArray(arr)) return;

      // Look for arrays that resemble messages:
      // - contain strings with text
      // - have role markers (0=user, 1=model)
      for (const item of arr) {
        if (Array.isArray(item)) {
          // Is this a message array?
          if (item.length >= 2 && typeof item[0] === "string" && item[0].length > 2) {
            // Might be the message text
            const possibleRole = findNearbyRole(arr);
            if (possibleRole !== null) {
              conversation.messages.push({
                role: possibleRole === 0 ? "user" : "model",
                content: item[0],
                timestamp: findNearbyTimestamp(arr),
              });
            }
          }
          walk(item, depth + 1);
        }
      }
    }

    walk(data, 0);
  }

  function findNearbyRole(arr) {
    // Look for a numeric role marker near the data
    for (const item of arr) {
      if (item === 0 || item === 1) return item;
    }
    return null;
  }

  function findNearbyTimestamp(arr) {
    // Look for a timestamp (a large ~13-digit number)
    for (const item of arr) {
      if (typeof item === "number" && item > 1600000000000 && item < 2000000000000) {
        return item;
      }
    }
    return null;
  }

  function extractMessagesFromRenderedDOM(doc, conversation) {
    // Fallback: extract from the rendered DOM
    // Gemini uses message-container elements
    const messageEls = doc.querySelectorAll(
      '[class*="message"], [class*="response"], [class*="query"], ' +
      '[data-message-id], .conversation-turn'
    );

    for (const el of messageEls) {
      const text = el.textContent?.trim();
      if (!text || text.length < 2) continue;

      // Determine the role by CSS classes or position
      const classList = el.className || "";
      const isUser =
        classList.includes("user") ||
        classList.includes("query") ||
        classList.includes("human");

      conversation.messages.push({
        role: isUser ? "user" : "model",
        content: text,
        timestamp: null,
        source: "dom_fallback",
      });
    }
  }

  // ==================== CANVAS EXTRACTION ====================

  function extractCanvasFromPage(doc, conversation) {
    // Look for Canvas artifacts in the page
    // Canvas is usually rendered as an iframe or a separate section
    const canvasEls = doc.querySelectorAll(
      '[class*="canvas"], [class*="artifact"], [class*="immersive"], ' +
      'iframe[src*="canvas"], [data-artifact-id]'
    );

    for (const el of canvasEls) {
      const artifactId = el.getAttribute("data-artifact-id") || `canvas_${Date.now()}_${Math.random().toString(36).substr(2, 6)}`;
      const content = el.innerHTML || el.textContent || "";

      if (content.length > 5) {
        conversation.canvas_artifacts.push({
          id: artifactId,
          type: detectCanvasType(content),
          content: cleanCanvasContent(content),
          raw_html: content,
          parent_message_index: findParentMessageIndex(el, conversation),
        });
        state.stats.canvasItems++;
      }
    }
  }

  function detectCanvasType(content) {
    if (content.includes("<pre>") || content.includes("<code>") || content.includes("```"))
      return "code";
    if (content.includes("<table>") || content.includes("<th>"))
      return "table";
    if (content.includes("<h1>") || content.includes("<h2>") || content.length > 500)
      return "document";
    return "text";
  }

  function cleanCanvasContent(html) {
    // Convert HTML Canvas content into clean text/Markdown
    const doc = new DOMParser().parseFromString(html, "text/html");

    // Replace code blocks
    for (const code of doc.querySelectorAll("pre, code")) {
      const lang = code.className?.match(/language-(\w+)/)?.[1] || "";
      code.textContent = "```" + lang + "\n" + code.textContent + "\n```";
    }

    // Replace headings
    for (let i = 1; i <= 6; i++) {
      for (const h of doc.querySelectorAll(`h${i}`)) {
        h.textContent = "#".repeat(i) + " " + h.textContent + "\n";
      }
    }

    // Replace lists
    for (const li of doc.querySelectorAll("li")) {
      li.textContent = "- " + li.textContent;
    }

    // Replace bold/italic
    for (const b of doc.querySelectorAll("b, strong")) {
      b.textContent = "**" + b.textContent + "**";
    }
    for (const i of doc.querySelectorAll("i, em")) {
      i.textContent = "*" + i.textContent + "*";
    }

    return doc.body.textContent?.trim() || html;
  }

  function findParentMessageIndex(el, conversation) {
    // Find the nearest message the Canvas belongs to
    let parent = el.parentElement;
    let depth = 0;
    while (parent && depth < 10) {
      const msgId = parent.getAttribute("data-message-id");
      if (msgId) {
        const idx = conversation.messages.findIndex(
          (m) => m.id === msgId || m.content?.includes(msgId)
        );
        return idx >= 0 ? idx : null;
      }
      parent = parent.parentElement;
      depth++;
    }
    return null;
  }

  // ==================== MAIN EXTRACTION FLOW ====================

  async function extractAll() {
    if (state.isExtracting) {
      log("⚠ Extraction already in progress.");
      return;
    }

    state.isExtracting = true;
    state.cancelRequested = false;
    state.stats = { total: 0, downloaded: 0, errors: 0, canvasItems: 0 };
    state.conversations.clear();

    updateUI("extracting");

    try {
      // 1. Get the page tokens
      log("🔑 Extracting authorization tokens...");
      const tokens = extractPageTokens();
      state.csrfToken = tokens.at;
      state.buildLabel = tokens.bl;
      state.accountEmail = tokens.email;

      if (!tokens.at) {
        log("⚠ CSRF token not found. Try reloading the page.");
        log("   Trying to proceed without a token (DOM method only)...");
      } else {
        log(`✓ Token found. Account: ${tokens.email || "unknown"}`);
      }
      Object.assign(state, tokens);

      // 2. Collect the list of chats
      log("\n📋 Collecting the chat list...");
      const convListDOM = extractConversationsFromDOM();
      const convListPage = extractConversationsFromPageData();

      // Merge and deduplicate
      const allConvs = new Map();
      for (const c of [...convListDOM, ...convListPage]) {
        if (!allConvs.has(c.id)) {
          allConvs.set(c.id, c);
        }
      }

      // Method 3: if the list is empty — try scrolling the sidebar
      if (allConvs.size === 0) {
        log("ℹ Chat list empty from DOM/data. Trying to scroll the sidebar...");
        await scrollSidebarToLoadAll();
        const afterScroll = extractConversationsFromDOM();
        for (const c of afterScroll) {
          if (!allConvs.has(c.id)) allConvs.set(c.id, c);
        }
      }

      state.stats.total = allConvs.size;
      log(`✓ Chats found: ${allConvs.size}`);

      if (allConvs.size === 0) {
        log("\n⚠ No chats found. Possible reasons:");
        log("  - No chats in this account");
        log("  - Google changed the page structure");
        log("  - You need to open at least one chat before exporting");
        state.isExtracting = false;
        updateUI("idle");
        return;
      }

      // 3. Download the content of each chat
      log("\n📥 Downloading chat content...\n");

      const convIds = Array.from(allConvs.keys());
      for (let i = 0; i < convIds.length; i++) {
        if (state.cancelRequested) {
          log("\n⛔ Extraction cancelled by the user.");
          break;
        }

        const convId = convIds[i];
        const convMeta = allConvs.get(convId);
        const progress = `[${i + 1}/${convIds.length}]`;

        log(`${progress} ${convMeta.title || convId}`);
        updateProgress(i + 1, convIds.length);

        let retries = 0;
        let success = false;

        while (retries <= CONFIG.MAX_RETRIES && !success) {
          try {
            const data = await fetchConversationContent(convId);
            if (data && (data.messages.length > 0 || data.canvas_artifacts.length > 0)) {
              data.title = data.title || convMeta.title || `Chat ${convId.substring(0, 8)}`;
              state.conversations.set(convId, data);
              state.stats.downloaded++;
              success = true;
              log(`  ✓ ${data.messages.length} messages, ${data.canvas_artifacts.length} artifacts`);
            } else if (data) {
              // Empty chat — keep the metadata
              data.title = convMeta.title || `Empty chat ${convId.substring(0, 8)}`;
              state.conversations.set(convId, data);
              state.stats.downloaded++;
              success = true;
              log("  ✓ (empty chat or no extractable data)");
            } else {
              throw new Error("No data");
            }
          } catch (err) {
            retries++;
            if (retries <= CONFIG.MAX_RETRIES) {
              const delay = CONFIG.RETRY_BACKOFF_MS * retries;
              log(`  ⚠ Error: ${err.message}. Retry ${retries}/${CONFIG.MAX_RETRIES} in ${delay / 1000}s...`);
              await sleep(delay);
            } else {
              log(`  ❌ Failed to download after ${CONFIG.MAX_RETRIES} attempts: ${err.message}`);
              state.stats.errors++;
            }
          }
        }

        // Delay between requests — to avoid triggering rate limiting
        if (i < convIds.length - 1) {
          await sleep(CONFIG.DELAY_BETWEEN_REQUESTS_MS);
        }
      }

      // 4. Build and download the result
      log("\n📦 Building the export file...");
      const exportData = buildExportData();
      downloadJSON(exportData);

      log(`\n✅ Export complete!`);
      log(`   Chats downloaded: ${state.stats.downloaded}`);
      log(`   Canvas artifacts: ${state.stats.canvasItems}`);
      log(`   Errors: ${state.stats.errors}`);
      log(`   File saved to your Downloads folder.`);
    } catch (err) {
      log(`\n❌ Critical error: ${err.message}`);
      console.error("Chatrove error:", err);
    } finally {
      state.isExtracting = false;
      updateUI("done");
    }
  }

  async function scrollSidebarToLoadAll() {
    // Scroll the sidebar to the bottom so all chats load
    const sidebar = document.querySelector(
      '[class*="sidebar"], [class*="nav"], [role="navigation"]'
    );
    if (!sidebar) return;

    let prevCount = 0;
    let sameCountStreak = 0;

    for (let i = 0; i < 50; i++) {
      sidebar.scrollTop = sidebar.scrollHeight;
      await sleep(500);

      const curCount = document.querySelectorAll(
        'a[href*="/app/"], a[href*="/c/"]'
      ).length;
      if (curCount === prevCount) {
        sameCountStreak++;
        if (sameCountStreak >= 3) break;
      } else {
        sameCountStreak = 0;
        prevCount = curCount;
      }
    }
  }

  function buildExportData() {
    const conversations = [];
    for (const [id, conv] of state.conversations) {
      conversations.push({
        id,
        title: conv.title,
        messages: conv.messages,
        canvas_artifacts: conv.canvas_artifacts,
        created_time: conv.created_time,
        updated_time: conv.updated_time,
        message_count: conv.messages.length,
        canvas_count: conv.canvas_artifacts.length,
      });
    }

    // Sort by update time (newest first)
    conversations.sort((a, b) => {
      const ta = a.updated_time || a.created_time || 0;
      const tb = b.updated_time || b.created_time || 0;
      return tb - ta;
    });

    return {
      export_metadata: {
        version: CONFIG.VERSION,
        export_date: new Date().toISOString(),
        account_email: state.accountEmail || "unknown",
        total_conversations: conversations.length,
        total_messages: conversations.reduce((s, c) => s + c.message_count, 0),
        total_canvas_artifacts: conversations.reduce((s, c) => s + c.canvas_count, 0),
        errors: state.stats.errors,
        source: "gemini_vault_extractor",
      },
      conversations,
    };
  }

  function downloadJSON(data) {
    const json = JSON.stringify(data, null, 2);
    const blob = new Blob([json], { type: "application/json" });
    const url = URL.createObjectURL(blob);

    const dateStr = new Date().toISOString().replace(/[:T]/g, "-").substring(0, 19);
    const email = (state.accountEmail || "unknown").replace(/[@.]/g, "_");
    const filename = `${CONFIG.EXPORT_FILENAME_PREFIX}_${email}_${dateStr}.json`;

    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);

    log(`💾 File: ${filename} (${(json.length / 1024 / 1024).toFixed(1)} MB)`);
  }

  // ==================== UI ====================

  let uiPanel = null;
  let logArea = null;
  let progressBar = null;
  let progressText = null;

  function createUI() {
    if (document.getElementById("gemini-vault-panel")) return;

    uiPanel = document.createElement("div");
    uiPanel.id = "gemini-vault-panel";
    uiPanel.innerHTML = `
      <style>
        #gemini-vault-panel {
          position: fixed;
          bottom: 20px;
          right: 20px;
          width: 420px;
          max-height: 500px;
          background: #1e1e2e;
          border: 1px solid #45475a;
          border-radius: 12px;
          box-shadow: 0 8px 32px rgba(0,0,0,0.4);
          z-index: 999999;
          font-family: 'Segoe UI', system-ui, sans-serif;
          font-size: 13px;
          color: #cdd6f4;
          display: flex;
          flex-direction: column;
          overflow: hidden;
        }
        #gv-header {
          display: flex;
          align-items: center;
          justify-content: space-between;
          padding: 10px 14px;
          background: #313244;
          border-bottom: 1px solid #45475a;
          cursor: move;
        }
        #gv-header h3 {
          margin: 0;
          font-size: 14px;
          font-weight: 600;
          color: #89b4fa;
        }
        #gv-header .gv-close {
          cursor: pointer;
          color: #6c7086;
          font-size: 18px;
          line-height: 1;
          padding: 0 4px;
        }
        #gv-header .gv-close:hover { color: #f38ba8; }
        #gv-buttons {
          display: flex;
          gap: 8px;
          padding: 10px 14px;
          border-bottom: 1px solid #45475a;
        }
        .gv-btn {
          flex: 1;
          padding: 8px 12px;
          border: none;
          border-radius: 8px;
          cursor: pointer;
          font-size: 13px;
          font-weight: 500;
          transition: all 0.2s;
        }
        .gv-btn-primary {
          background: #89b4fa;
          color: #1e1e2e;
        }
        .gv-btn-primary:hover { background: #74c7ec; }
        .gv-btn-primary:disabled {
          background: #45475a;
          color: #6c7086;
          cursor: not-allowed;
        }
        .gv-btn-danger {
          background: #f38ba8;
          color: #1e1e2e;
        }
        .gv-btn-danger:hover { background: #eba0ac; }
        #gv-progress {
          padding: 0 14px 8px;
          display: none;
        }
        #gv-progress-bar {
          width: 100%;
          height: 6px;
          background: #313244;
          border-radius: 3px;
          overflow: hidden;
        }
        #gv-progress-fill {
          height: 100%;
          background: linear-gradient(90deg, #89b4fa, #74c7ec);
          border-radius: 3px;
          width: 0%;
          transition: width 0.3s;
        }
        #gv-progress-text {
          font-size: 11px;
          color: #6c7086;
          margin-top: 4px;
        }
        #gv-log {
          flex: 1;
          overflow-y: auto;
          padding: 8px 14px;
          font-family: 'Cascadia Code', 'Consolas', monospace;
          font-size: 11px;
          line-height: 1.5;
          max-height: 280px;
          white-space: pre-wrap;
          word-break: break-word;
        }
        #gv-log::-webkit-scrollbar {
          width: 6px;
        }
        #gv-log::-webkit-scrollbar-thumb {
          background: #45475a;
          border-radius: 3px;
        }
        #gv-minimized {
          position: fixed;
          bottom: 20px;
          right: 20px;
          width: 48px;
          height: 48px;
          background: #89b4fa;
          border-radius: 50%;
          display: none;
          align-items: center;
          justify-content: center;
          cursor: pointer;
          box-shadow: 0 4px 16px rgba(0,0,0,0.3);
          z-index: 999999;
          font-size: 22px;
          transition: transform 0.2s;
        }
        #gv-minimized:hover { transform: scale(1.1); }
      </style>

      <div id="gv-header">
        <h3>Chatrove</h3>
        <span class="gv-close" id="gv-minimize" title="Minimize">─</span>
      </div>
      <div id="gv-buttons">
        <button class="gv-btn gv-btn-primary" id="gv-start">Export all chats</button>
        <button class="gv-btn gv-btn-danger" id="gv-cancel" style="display:none">Cancel</button>
      </div>
      <div id="gv-progress">
        <div id="gv-progress-bar"><div id="gv-progress-fill"></div></div>
        <div id="gv-progress-text"></div>
      </div>
      <div id="gv-log">Chatrove v${CONFIG.VERSION}\nReady to export. Click the button above.\n</div>
    `;

    const minimizedBtn = document.createElement("div");
    minimizedBtn.id = "gv-minimized";
    minimizedBtn.textContent = "📦";
    minimizedBtn.title = "Chatrove";

    document.body.appendChild(uiPanel);
    document.body.appendChild(minimizedBtn);

    // Handlers
    logArea = uiPanel.querySelector("#gv-log");
    progressBar = uiPanel.querySelector("#gv-progress-fill");
    progressText = uiPanel.querySelector("#gv-progress-text");

    uiPanel.querySelector("#gv-start").addEventListener("click", extractAll);
    uiPanel.querySelector("#gv-cancel").addEventListener("click", () => {
      state.cancelRequested = true;
    });
    uiPanel.querySelector("#gv-minimize").addEventListener("click", () => {
      uiPanel.style.display = "none";
      minimizedBtn.style.display = "flex";
    });
    minimizedBtn.addEventListener("click", () => {
      minimizedBtn.style.display = "none";
      uiPanel.style.display = "flex";
    });

    // Drag
    makeDraggable(uiPanel, uiPanel.querySelector("#gv-header"));
  }

  function makeDraggable(el, handle) {
    let offsetX, offsetY, isDragging = false;
    handle.addEventListener("mousedown", (e) => {
      isDragging = true;
      offsetX = e.clientX - el.getBoundingClientRect().left;
      offsetY = e.clientY - el.getBoundingClientRect().top;
      e.preventDefault();
    });
    document.addEventListener("mousemove", (e) => {
      if (!isDragging) return;
      el.style.left = (e.clientX - offsetX) + "px";
      el.style.top = (e.clientY - offsetY) + "px";
      el.style.right = "auto";
      el.style.bottom = "auto";
    });
    document.addEventListener("mouseup", () => {
      isDragging = false;
    });
  }

  function log(msg) {
    console.log("[Chatrove]", msg);
    if (logArea) {
      logArea.textContent += msg + "\n";
      logArea.scrollTop = logArea.scrollHeight;
    }
  }

  function updateProgress(done, total) {
    const pct = total > 0 ? Math.round((done / total) * 100) : 0;
    if (progressBar) progressBar.style.width = pct + "%";
    if (progressText) progressText.textContent = `${done} / ${total} (${pct}%)`;
  }

  function updateUI(mode) {
    if (!uiPanel) return;
    const startBtn = uiPanel.querySelector("#gv-start");
    const cancelBtn = uiPanel.querySelector("#gv-cancel");
    const progressDiv = uiPanel.querySelector("#gv-progress");

    switch (mode) {
      case "extracting":
        startBtn.disabled = true;
        startBtn.style.display = "none";
        cancelBtn.style.display = "block";
        progressDiv.style.display = "block";
        break;
      case "done":
      case "idle":
        startBtn.disabled = false;
        startBtn.style.display = "block";
        cancelBtn.style.display = "none";
        if (mode === "idle") progressDiv.style.display = "none";
        break;
    }
  }

  // ==================== INIT ====================

  function init() {
    // Wait for the page to fully load
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", init);
      return;
    }

    // Make sure we are on Gemini
    if (!window.location.hostname.includes("gemini.google.com")) {
      return;
    }

    // Give the SPA time to load
    setTimeout(() => {
      createUI();
      log("ℹ Panel loaded. Make sure your chats are visible in the sidebar.");
    }, 2000);
  }

  init();
})();
