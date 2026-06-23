/**
 * Chatrove Extractor — Console Edition
 *
 * How to use:
 * 1. Open https://gemini.google.com/
 * 2. Make sure you are signed in and can see your chats in the sidebar
 * 3. Press F12 → Console
 * 4. Copy this ENTIRE script and paste it into the console
 * 5. Press Enter
 * 6. The export starts automatically. The JSON file downloads when finished.
 *
 * To cancel: window.__geminiVaultCancel = true
 */

(async function GeminiVaultConsoleExtractor() {
  "use strict";

  const DELAY_MS = 1500;
  const MAX_RETRIES = 3;
  const RETRY_BACKOFF_MS = 3000;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  window.__geminiVaultCancel = false;

  console.log("%c[Chatrove] v1.0.0 — Chat export", "color:#89b4fa;font-size:14px;font-weight:bold");
  console.log("[GV] To cancel: window.__geminiVaultCancel = true");

  // ── Tokens ──
  function getTokens() {
    let at = null, bl = null, email = null;

    if (typeof WIZ_global_data !== "undefined") {
      at = WIZ_global_data.SNlM0e;
      bl = WIZ_global_data.cfb2h;
    }

    if (!at) {
      for (const s of document.querySelectorAll("script")) {
        const t = s.textContent || "";
        const am = t.match(/SNlM0e":"([^"]+)"/);
        if (am) at = am[1];
        const bm = t.match(/cfb2h":"([^"]+)"/);
        if (bm) bl = bm[1];
        const em = t.match(/["\s]([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})["\s]/);
        if (em && !email) email = em[1];
        if (at && bl && email) break;
      }
    }

    return { at, bl, email };
  }

  // ── Collect the chat list ──
  function getConversationIds() {
    const ids = new Map();

    // From the DOM
    for (const el of document.querySelectorAll('a[href*="/app/"], a[href*="/c/"], [data-conversation-id]')) {
      const dataId = el.getAttribute("data-conversation-id");
      const href = el.getAttribute("href") || "";
      let cid = dataId;
      if (!cid) {
        const m = href.match(/\/(?:app|c)\/([a-f0-9-]+)/i);
        if (m) cid = m[1];
      }
      if (cid && !ids.has(cid)) {
        ids.set(cid, (el.textContent || "").trim().substring(0, 200));
      }
    }

    // From the page data
    for (const s of document.querySelectorAll("script")) {
      const t = s.textContent || "";
      const re = /["\[]([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})["\]]/gi;
      let m;
      while ((m = re.exec(t)) !== null) {
        if (!ids.has(m[1])) ids.set(m[1], "");
      }
    }

    return ids;
  }

  // ── Scroll the sidebar ──
  async function scrollSidebar() {
    const sidebar = document.querySelector('[class*="sidebar"], [class*="nav"], [role="navigation"]');
    if (!sidebar) return;
    let prev = 0, streak = 0;
    for (let i = 0; i < 50 && streak < 3; i++) {
      sidebar.scrollTop = sidebar.scrollHeight;
      await sleep(500);
      const cur = document.querySelectorAll('a[href*="/app/"], a[href*="/c/"]').length;
      if (cur === prev) streak++;
      else { streak = 0; prev = cur; }
    }
  }

  // ── Fetch a chat ──
  async function fetchConversation(convId) {
    const resp = await fetch(`/app/${convId}`, { credentials: "include", headers: { Accept: "text/html" } });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const html = await resp.text();

    const doc = new DOMParser().parseFromString(html, "text/html");
    const conv = {
      id: convId,
      title: (doc.querySelector("title")?.textContent || "").replace(" - Gemini", "").trim(),
      messages: [],
      canvas_artifacts: [],
      created_time: null,
      updated_time: null,
    };

    // Extract from AF_initDataCallback
    for (const s of doc.querySelectorAll("script")) {
      const t = s.textContent || "";
      const cbs = t.matchAll(/AF_initDataCallback\s*\(\s*\{[^}]*data:\s*(\[[\s\S]*?\])\s*\}\s*\)/g);
      for (const cb of cbs) {
        try { walkData(JSON.parse(cb[1]), conv); } catch (_) {}
      }
    }

    // Fallback: DOM
    if (conv.messages.length === 0) {
      for (const el of doc.querySelectorAll('[class*="message"], [class*="response"], [class*="query"], [data-message-id]')) {
        const text = el.textContent?.trim();
        if (!text || text.length < 2) continue;
        const cls = el.className || "";
        conv.messages.push({
          role: cls.includes("user") || cls.includes("query") ? "user" : "model",
          content: text,
          timestamp: null,
          source: "dom",
        });
      }
    }

    // Canvas
    for (const el of doc.querySelectorAll('[class*="canvas"], [class*="artifact"], [class*="immersive"]')) {
      const content = el.innerHTML || el.textContent || "";
      if (content.length > 5) {
        conv.canvas_artifacts.push({
          id: `canvas_${Date.now()}_${Math.random().toString(36).substr(2, 6)}`,
          type: content.includes("<code>") ? "code" : content.includes("<table>") ? "table" : "document",
          content: el.textContent?.trim() || content,
          raw_html: content.substring(0, 50000),
        });
      }
    }

    return conv;
  }

  function walkData(arr, conv, depth = 0) {
    if (depth > 10 || !Array.isArray(arr)) return;
    for (const item of arr) {
      if (Array.isArray(item)) {
        if (item.length >= 2 && typeof item[0] === "string" && item[0].length > 2) {
          const role = arr.find((x) => x === 0 || x === 1);
          if (role !== undefined) {
            const ts = arr.find((x) => typeof x === "number" && x > 1600000000000 && x < 2000000000000);
            conv.messages.push({ role: role === 0 ? "user" : "model", content: item[0], timestamp: ts || null });
          }
        }
        walkData(item, conv, depth + 1);
      }
    }
  }

  // ── Main flow ──
  const tokens = getTokens();
  console.log(`[GV] Account: ${tokens.email || "?"} | Token: ${tokens.at ? "✓" : "✗"}`);

  let ids = getConversationIds();
  if (ids.size === 0) {
    console.log("[GV] No chats found in the DOM. Scrolling the sidebar...");
    await scrollSidebar();
    ids = getConversationIds();
  }

  console.log(`[GV] Chats found: ${ids.size}`);
  if (ids.size === 0) {
    console.error("[GV] No chats found. Make sure the sidebar is open and the chats are loaded.");
    return;
  }

  const conversations = [];
  let downloaded = 0, errors = 0;
  const entries = Array.from(ids.entries());

  for (let i = 0; i < entries.length; i++) {
    if (window.__geminiVaultCancel) {
      console.warn("[GV] Cancelled by the user.");
      break;
    }

    const [cid, title] = entries[i];
    const pct = Math.round(((i + 1) / entries.length) * 100);
    console.log(`[GV] [${i + 1}/${entries.length}] ${pct}% — ${title || cid.substring(0, 12)}`);

    let ok = false;
    for (let retry = 0; retry <= MAX_RETRIES && !ok; retry++) {
      try {
        const data = await fetchConversation(cid);
        data.title = data.title || title || `Chat ${cid.substring(0, 8)}`;
        conversations.push(data);
        downloaded++;
        ok = true;
      } catch (err) {
        if (retry < MAX_RETRIES) {
          console.warn(`[GV]   Retry ${retry + 1}: ${err.message}`);
          await sleep(RETRY_BACKOFF_MS * (retry + 1));
        } else {
          console.error(`[GV]   FAILED: ${err.message}`);
          errors++;
        }
      }
    }

    if (i < entries.length - 1) await sleep(DELAY_MS);
  }

  // Sort
  conversations.sort((a, b) => (b.updated_time || 0) - (a.updated_time || 0));

  const exportData = {
    export_metadata: {
      version: "1.0.0",
      export_date: new Date().toISOString(),
      account_email: tokens.email || "unknown",
      total_conversations: conversations.length,
      total_messages: conversations.reduce((s, c) => s + c.messages.length, 0),
      total_canvas_artifacts: conversations.reduce((s, c) => s + c.canvas_artifacts.length, 0),
      errors,
      source: "gemini_vault_console",
    },
    conversations,
  };

  // Download
  const json = JSON.stringify(exportData, null, 2);
  const blob = new Blob([json], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const dateStr = new Date().toISOString().replace(/[:T]/g, "-").substring(0, 19);
  const email = (tokens.email || "unknown").replace(/[@.]/g, "_");
  const fname = `gemini_vault_export_${email}_${dateStr}.json`;
  const a = document.createElement("a");
  a.href = url; a.download = fname;
  document.body.appendChild(a); a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);

  console.log(`%c[GV] ✅ Done! ${downloaded} chats → ${fname} (${(json.length/1024/1024).toFixed(1)} MB)`, "color:#a6e3a1;font-size:13px;font-weight:bold");
  if (errors > 0) console.warn(`[GV] Errors: ${errors}`);
})();
