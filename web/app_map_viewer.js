// app_map_viewer.html 的程式（由 viewer_server.py 提供，跟網頁放在同一個資料夾）。
// 整支包在一個立即執行的函式裡，變數不會漏到全域；頁面元素都在 HTML，這裡只綁事件、畫畫面。
(function () {
  "use strict";
  var DATA = JSON.parse(document.getElementById("map-data").textContent);
  var screens = DATA.screens, edges = DATA.edges, skipped = DATA.skipped, app = DATA.app;
  var byUid = {}, elStatus = {}, edgesByFrom = {}, edgesByTo = {}, skippedByScreen = {};
  var selectedUid = null, nodeGroups = {};
  // 目前地圖是從哪個檔案載入的，給「測試情境產生」「測案編輯」「測案執行」
  // 顯示資料來源用；頁面內嵌的資料沒有檔名
  // 目前開啟的 workspace 專案（{id, name, platform, bundle_id}），null＝沒用專案；
  // mapVersion 是上次讀到／存檔後的地圖版本，存檔時帶回去讓伺服器擋掉互相覆蓋
  var currentProject = null, mapVersion = null, projectList = [], projectCases = [], projectBatches = [];
  var projectVars = [];  // 專案變數檔清單：[{name, keys: [...], sensitive}]（值要另外讀單一檔）
  // 專案裡目前開著的地圖（一個專案可以有很多張，依功能分開爬）
  var currentMapName = null, projectMaps = [];

  // 各分頁的「目前使用」標示。kind 是來源類型（例如「目前地圖」「上傳檔案」），
  // name 是檔名或補充說明；kind 為 null 代表還沒有資料。
  // 有傳 onReset 代表目前不是用目前地圖，多一顆「改回目前地圖」
  function setSourceLine(el, label, kind, name, onReset) {
    el.className = "source-line" + (kind ? "" : " empty");
    el.innerHTML = '<span>' + escapeHtml(label) + '：</span>' +
      '<span class="source-kind">' + escapeHtml(kind || "尚未選擇") + '</span>' +
      (name ? '<span class="source-name">' + escapeHtml(name) + '</span>' : "");
    if (onReset) {
      var btn = document.createElement("button");
      btn.className = "btn";
      btn.textContent = "↺ 改回目前地圖";
      btn.addEventListener("click", onReset);
      el.appendChild(btn);
    }
  }
  // ---------------------------------------------------------------
  // 平台（iOS／Android）：地圖自己記錄是哪個平台掃出來的（app.platform），
  // 舊地圖沒有這個欄位就從元件 class 推斷——iOS 是 XCUIElementTypeXxx，
  // Android 是 android.widget.Xxx 這種完整路徑
  // ---------------------------------------------------------------
  function inferPlatformFromClasses(classes) {
    for (var i = 0; i < classes.length; i++) {
      var c = classes[i] || "";
      if (c.indexOf("XCUIElementType") === 0) return "ios";
      if (c.indexOf(".") !== -1) return "android";
    }
    return null;
  }
  function mapPlatform() {
    if (app && app.platform) return app.platform;
    var classes = [];
    screens.forEach(function (s) { (s.els || []).forEach(function (el) { classes.push(el.c); }); });
    return inferPlatformFromClasses(classes);
  }
  // 上傳的 element_map.json（全名欄位）：Android 掃描會帶 platform，iOS 舊資料沒有就看 class
  function elementMapPlatform(elementMap) {
    var classes = [];
    var list = (elementMap && elementMap.screens) || [];
    for (var i = 0; i < list.length; i++) {
      if (list[i].platform) return list[i].platform;
      (list[i].elements || []).forEach(function (el) { classes.push(el.class); });
    }
    return inferPlatformFromClasses(classes);
  }
  function platformName(p) { return p === "android" ? "Android" : p === "ios" ? "iOS" : "未知"; }
  function shortClass(c) {
    c = c || "";
    return c.indexOf("XCUIElementType") === 0 ? c.slice("XCUIElementType".length) : c.split(".").pop();
  }
  // 網頁由 viewer_server.py 直接提供（http://127.0.0.1:8767/），API 跟網頁同源，
  // 一律用相對路徑呼叫，不用再填伺服器網址
  var API_BASE = "";
  if (!/^https?:$/.test(location.protocol)) document.getElementById("fileProtocolWarning").hidden = false;
  // /ping 回傳伺服器目前接的平台（aitest-server --platform），舊版伺服器沒有這個欄位就是 null
  async function fetchServerPlatform(base) {
    var res = await fetch(base + "/ping");
    if (!res.ok) throw new Error("HTTP " + res.status);
    return (await res.json()).platform || null;
  }
  // 操作手機前先確認資料的平台跟伺服器接的手機一致：兩邊定位規則完全不同，
  // 硬送過去不只會找不到元件，座標備援還可能在錯的手機上亂點
  async function assertPlatformMatches(base, dataPlatform) {
    var serverPlatform = await fetchServerPlatform(base);
    if (dataPlatform && serverPlatform && dataPlatform !== serverPlatform) {
      var err = new Error("資料是 " + platformName(dataPlatform) + "，但 viewer_server 目前接的是 " +
        platformName(serverPlatform) + "，請改用 --platform " + dataPlatform + " 重新啟動伺服器");
      err.platformMismatch = true;
      throw err;
    }
    return serverPlatform;
  }
  // 失敗訊息：平台不符不是連線問題，不要套「伺服器有啟動嗎？」這種提示誤導
  function failText(prefix, err, hint) {
    return err.platformMismatch ? "⚠️ " + err.message : prefix + "：" + err.message + hint;
  }
  async function renderServerStatus(base, statusEl, dataPlatform) {
    statusEl.textContent = "檢查中…"; statusEl.className = "conn-status unknown";
    try {
      var serverPlatform = await fetchServerPlatform(base);
      var mismatch = dataPlatform && serverPlatform && dataPlatform !== serverPlatform;
      statusEl.textContent = mismatch
        ? "已連線，但伺服器接的是 " + platformName(serverPlatform) + "，資料是 " + platformName(dataPlatform)
        : "已連線（" + platformName(serverPlatform) + "）";
      statusEl.className = "conn-status " + (mismatch ? "offline" : "online");
    } catch (err) {
      statusEl.textContent = "連不上"; statusEl.className = "conn-status offline";
    }
  }

  var svg = document.getElementById("tree");

  function key(su, eu) { return su + "\x01" + eu; }
  function svgEl(tag, attrs) {
    var el = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (var k in attrs) el.setAttribute(k, attrs[k]);
    return el;
  }

  // 重建整份地圖的所有衍生資料＋整棵SVG樹——首次載入呼叫一次，之後每次
  // 手動新增節點／新增邊都要整個重呼叫一次（screens/edges 陣列變了，
  // 版面、上下游關係、統計數字全部要重算，不能只是局部貼一塊新的進去）
  function rebuildAll() {
    byUid = {}; screens.forEach(function (s) { byUid[s.uid] = s; });

    // ---- header stats ----
    document.getElementById("bundleId").textContent = app.bundle_id;
    document.getElementById("crawledAt").textContent = app.crawled_at;
    document.getElementById("rootName").textContent = app.root_name;
    document.getElementById("mapPlatform").textContent = platformName(mapPlatform());
    document.getElementById("maxDepth").textContent = app.max_depth;
    document.getElementById("statScreens").textContent = screens.length;
    document.getElementById("statTree").textContent = edges.filter(function (e) { return e.kind === "tree"; }).length;
    document.getElementById("statCross").textContent = edges.filter(function (e) { return e.kind === "cross-link"; }).length;
    document.getElementById("statNoop").textContent = edges.filter(function (e) { return e.kind === "noop"; }).length;
    document.getElementById("statSkip").textContent = skipped.length;

    // ---- classification lookup: screenUid::elementUid -> {kind, to, reason} ----
    elStatus = {};
    edges.forEach(function (e) {
      elStatus[key(e.from, e.via)] = { type: "edge", kind: e.kind, to: e.to };
    });
    skipped.forEach(function (s) {
      var k = key(s.screen_uid, s.element_uid);
      if (!elStatus[k]) elStatus[k] = { type: "skip", kind: s.reason, to: null };
    });

    edgesByFrom = {}; edgesByTo = {};
    edges.forEach(function (e) {
      (edgesByFrom[e.from] = edgesByFrom[e.from] || []).push(e);
      if (e.to) (edgesByTo[e.to] = edgesByTo[e.to] || []).push(e);
    });
    skippedByScreen = {};
    skipped.forEach(function (s) { (skippedByScreen[s.screen_uid] = skippedByScreen[s.screen_uid] || []).push(s); });

    if (screens.length === 0) {
      svg.setAttribute("viewBox", "0 0 200 80");
      svg.setAttribute("width", 200);
      svg.setAttribute("height", 80);
      svg.innerHTML = "";
      nodeGroups = {};
      return;
    }

    // 依顯示寬度截斷：中文、全形字約佔兩個英數字寬
    function fitNodeName(name, maxUnits) {
      var units = 0, out = "";
      for (var i = 0; i < name.length; i++) {
        var u = name.charCodeAt(i) >= 0x2E80 ? 2 : 1;
        if (units + u > maxUnits) return out + "\u2026";
        units += u; out += name[i];
      }
      return out;
    }

    // ---- layout: rows by depth ----
    var byDepth = {};
    screens.forEach(function (s) { (byDepth[s.depth] = byDepth[s.depth] || []).push(s); });
    var depths = Object.keys(byDepth).map(Number).sort(function (a, b) { return a - b; });

    var NODE_W = 200, NODE_H = 74, COL_GAP = 34, ROW_GAP = 68, PAD = 20;
    var maxCols = Math.max.apply(null, depths.map(function (d) { return byDepth[d].length; }));
    var rowWidth = maxCols * NODE_W + (maxCols - 1) * COL_GAP;
    var svgW = rowWidth + PAD * 2;
    var svgH = depths.length * (NODE_H + ROW_GAP) + PAD * 2 - ROW_GAP + PAD;

    var pos = {}; // uid -> {x,y} center-top anchor (x = center, y = top)
    depths.forEach(function (d, rowIdx) {
      var rowScreens = byDepth[d].slice().sort(function (a, b) {
        return (a.name || "").localeCompare(b.name || "");
      });
      var n = rowScreens.length;
      var rw = n * NODE_W + (n - 1) * COL_GAP;
      var startX = PAD + (rowWidth - rw) / 2;
      rowScreens.forEach(function (s, i) {
        pos[s.uid] = { x: startX + i * (NODE_W + COL_GAP) + NODE_W / 2, y: PAD + rowIdx * (NODE_H + ROW_GAP) };
      });
    });

    svg.setAttribute("viewBox", "0 0 " + svgW + " " + svgH);
    svg.setAttribute("width", svgW);
    svg.setAttribute("height", svgH);
    svg.innerHTML = "";

    // ---- edges: tree lines (parent -> child) ----
    screens.forEach(function (s) {
      if (!s.parent || !pos[s.parent]) return;
      var p = pos[s.parent], c = pos[s.uid];
      var x1 = p.x, y1 = p.y + NODE_H, x2 = c.x, y2 = c.y;
      var midY = (y1 + y2) / 2;
      var d = "M " + x1 + " " + y1 + " C " + x1 + " " + midY + ", " + x2 + " " + midY + ", " + x2 + " " + (y2 - 8);
      svg.appendChild(svgEl("path", { d: d, class: "edge-line" }));
      svg.appendChild(svgEl("polygon", {
        points: (x2 - 5) + "," + (y2 - 8) + " " + (x2 + 5) + "," + (y2 - 8) + " " + x2 + "," + y2,
        class: "edge-arrow"
      }));
    });

    // ---- node cards ----
    nodeGroups = {};
    screens.forEach(function (s) {
      var p = pos[s.uid];
      var g = svgEl("g", { class: "node-card", "data-uid": s.uid, transform: "translate(" + (p.x - NODE_W / 2) + "," + p.y + ")" });
      nodeGroups[s.uid] = g;

      g.appendChild(svgEl("rect", { class: "body", x: 0, y: 0, width: NODE_W, height: NODE_H, rx: 10 }));

      var chip = svgEl("g", {});
      chip.appendChild(svgEl("rect", { class: "depth-chip", x: 8, y: 8, width: 40, height: 16, rx: 8 }));
      var chipText = svgEl("text", { class: "depth-chip-text", x: 28, y: 19.5, "text-anchor": "middle", "font-size": "10" });
      chipText.textContent = "d" + s.depth;
      chip.appendChild(chipText);
      g.appendChild(chip);

      var elCountText = svgEl("text", { class: "node-sub", x: NODE_W - 8, y: 19.5, "text-anchor": "end" });
      elCountText.textContent = s.els.length + " 元件" + (s._blank ? "（未掃描）" : "");
      g.appendChild(elCountText);

      var nameText = svgEl("text", { class: "node-name", x: 10, y: 42 });
      nameText.textContent = fitNodeName(s.name, 23);
      g.appendChild(nameText);
      // 名稱被截斷時，滑鼠移上去可以看完整名稱
      var tip = svgEl("title", {});
      tip.textContent = s.name;
      g.appendChild(tip);

      // badges row: cross-link / noop / chrome / manual counts
      var outEdges = edgesByFrom[s.uid] || [];
      var crossN = outEdges.filter(function (e) { return e.kind === "cross-link"; }).length;
      var noopN = outEdges.filter(function (e) { return e.kind === "noop"; }).length;
      var skipN = (skippedByScreen[s.uid] || []).length;
      var badges = [];
      if (s._blank) badges.push({ label: "手動新增", color: "var(--gold-soft)", ink: "var(--gold)" });
      if (crossN) badges.push({ label: crossN + " cross", color: "var(--gold-soft)", ink: "var(--gold)" });
      if (noopN) badges.push({ label: noopN + " noop", color: "var(--grey-soft)", ink: "var(--grey-mid)" });
      if (skipN) badges.push({ label: skipN + " chrome", color: "var(--danger-soft)", ink: "var(--danger)" });
      var bx = 10;
      badges.forEach(function (b) {
        var w = 13 + b.label.length * 5.4;
        g.appendChild(svgEl("rect", { x: bx, y: 52, width: w, height: 15, rx: 7.5, fill: b.color }));
        var bt = svgEl("text", { class: "badge-text", x: bx + w / 2, y: 62.5, "text-anchor": "middle", fill: b.ink });
        bt.textContent = b.label;
        g.appendChild(bt);
        bx += w + 5;
      });

      g.addEventListener("click", function () { selectScreen(s.uid); });
      svg.appendChild(g);
    });

    if (selectedUid && nodeGroups[selectedUid]) {
      nodeGroups[selectedUid].classList.add("selected");
    }
  }

  rebuildAll();

  // ---------------------------------------------------------------
  // 匯出／載入：這個頁面是離線的靜態HTML，瀏覽器沒有寫入本機檔案的權限，
  // 重新整理就會回到頁面裡內嵌的那份原始資料——手動新增／掃描的節點只
  // 存在瀏覽器記憶體裡，重新整理就不見了。匯出成JSON是目前唯一能真正
  // 保存下來的方式。匯出用的是專案正式 app_map_tree.json 的全名格式，
  // 可以直接拿去覆蓋/合併那個檔案；載入時則兩種格式都吃（全名或這個
  // 工具內部的精簡格式都會自動判斷、轉換）。
  // ---------------------------------------------------------------
  // 欄位轉換（匯出用，精簡→全名）：跟下面 fullScreenToShort 相反方向。有兩個
  // 欄位精簡格式本來就沒保留、無從還原，只能給合理預設值，並且老實在按鈕的
  // 說明文字裡標出來，不要假裝這是跟爬蟲算出來的一樣準：
  //   - elements[].enabled：短格式沒存這個欄位，一律填 true
  //   - screens[].skeleton：完整版是爬蟲從「原始畫面全部節點」算出來的模板
  //     簽章（含所有畫面上的元件，不只是留下來的候選），這裡手上只有篩選過
  //     的候選清單，算出來的東西不是同一份東西，直接填 null——如果之後
  //     crawl_app_tree.py 重新爬到同一個畫面，不會認得這是同一個節點，
  //     只能靠人工核對、合併。
  //
  // 從全名格式載入的資料會把原始物件藏在 _orig（不可列舉：JSON.stringify、
  // Object.assign 都看不到，送給 LLM 的資料不會變大），轉回全名時以它為底，
  // 爬蟲算的 skeleton、enabled、description_source 這些精簡格式沒有的欄位
  // 才不會在「載入→存檔」一輪之後被洗掉——專案地圖會自動存檔，這點很重要。
  function hideOrig(obj, orig) {
    Object.defineProperty(obj, "_orig", { value: orig, enumerable: false, writable: true, configurable: true });
    return obj;
  }
  function shortElToFullElement(el) {
    var orig = el._orig || {};
    var out = Object.assign({}, orig, {
      unique_id: el.u, id_source: el.src, native_id: el.n, class: el.c,
      label: el.l, value: el.v, bounds: el.b,
      enabled: orig.enabled !== undefined ? orig.enabled : true, accessible: !!el.a,
      fingerprint: el.fp || null,
      description: el.d || "",
      description_source: el.d === (orig.description || "") && orig.description_source
        ? orig.description_source : (el.d ? "llm_generated" : "skipped"),
      status: orig.status || "active"
    });
    if (el._orig && !el.fp && !("fingerprint" in orig)) delete out.fingerprint;  // 原本沒有的欄位不要多長出來
    // 人工自訂的 XPath 也要匯出，不然匯出→載入（或拿匯出的地圖去批次執行）就不見了
    delete out.custom_xpath;
    if (el.custom_xpath) out.custom_xpath = el.custom_xpath;
    return out;
  }
  function shortScreenToFull(s) {
    var orig = s._orig || {};
    var out = Object.assign({}, orig, {
      screen_uid: s.uid, screen_name: s.name,
      parent_screen_uid: s.parent, entry_element_uid: s.entry,
      depth: s.depth, path: s.path || [],
      skeleton: orig.skeleton || null,
      last_scanned: s._scannedAt ? new Date(s._scannedAt).toISOString() : (orig.last_scanned || null),
      screenshot: s.screenshot,
      screenshot_blocked: orig.screenshot_blocked || false,
      screen_stable_at_scan: orig.screen_stable_at_scan !== undefined ? orig.screen_stable_at_scan : null,
      screen_width: s.w, screen_height: s.h,
      elements: (s.els || []).map(shortElToFullElement)
    });
    // 手動新增、還沒掃描的節點：存檔再載入後要能繼續「掃描目前畫面」
    delete out.pending_scan;
    if (s._blank) out.pending_scan = true;
    return out;
  }

  document.getElementById("exportMapBtn").addEventListener("click", async function () {
    if (currentProject && currentMapName) {
      // 開著專案：先存檔，再從伺服器下載「地圖＋用到的截圖」zip，對方用「📂 匯入地圖」連截圖一起收
      if (!(await flushMapSave())) { alert("地圖還有修改存不進專案（看頁首的訊息），先處理好再匯出"); return; }
      var link = document.createElement("a");
      link.href = API_BASE + mapApi(currentMapName) + "/export";
      link.download = currentMapName + ".zip";
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      document.getElementById("mapSaveStatus").textContent = "已匯出「" + currentMapName + "」（含截圖的 zip）";
      return;
    }
    var exportData = { app: app, screens: screens.map(shortScreenToFull), edges: edges, skipped: skipped };
    var blob = new Blob([JSON.stringify(exportData, null, 2)], { type: "application/json" });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    var ts = new Date().toISOString().replace(/[:.]/g, "-");
    a.href = url;
    a.download = "app_map_tree_export_" + ts + ".json";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    document.getElementById("mapSaveStatus").textContent = "已匯出（全名格式，" + screens.length + " 個畫面；skeleton 欄位是 null，爬蟲重爬到同畫面不會自動認得）";
  });

  // 欄位轉換（載入用，全名→精簡）：專案正式的 app_map_tree.json
  // （crawl_app_tree.py 產出）用的是全名欄位（screen_uid/screen_name/
  // elements[]/unique_id...），跟這個工具內部用的精簡欄位（uid/name/els[]/
  // u...）不一樣，讀進來要先轉過一輪。
  function fullScreenToShort(fs) {
    var orig = Object.assign({}, fs);
    delete orig.elements;
    var out = {
      uid: fs.screen_uid, name: fs.screen_name,
      parent: fs.parent_screen_uid, entry: fs.entry_element_uid,
      depth: fs.depth, path: fs.path || [],
      nav_title: fs.skeleton ? fs.skeleton.nav_title : null,
      selected_tab: fs.skeleton ? fs.skeleton.selected_tab : null,
      screenshot: fs.screenshot, w: fs.screen_width, h: fs.screen_height,
      els: (fs.elements || []).map(toShortEl)
    };
    if (fs.pending_scan) out._blank = true;
    return hideOrig(out, orig);
  }

  function normalizeImportedMap(data) {
    var screensIn = data.screens || [];
    var isFullSchema = screensIn.length > 0 && screensIn[0].screen_uid !== undefined;
    return {
      app: data.app || app,
      screens: isFullSchema ? screensIn.map(fullScreenToShort) : screensIn,
      edges: data.edges || [],
      skipped: data.skipped || []
    };
  }

  // 換上一份新地圖（檔案載入、開專案都走這裡），各分頁跟著刷新
  function applyMapData(data) {
    var normalized = normalizeImportedMap(data);
    screens = normalized.screens; edges = normalized.edges; skipped = normalized.skipped; app = normalized.app;
    DATA.screens = screens; DATA.edges = edges; DATA.skipped = skipped; DATA.app = app;
    selectedUid = null;
    detailCounter.textContent = "";
    rebuildAll();
    var r = screens.filter(function (sc) { return !sc.parent; })[0] || screens[0];
    if (r) selectScreen(r.uid); else renderEmptyMapDetail();
    // 「測試情境產生」「測案編輯」「測案執行」都用目前地圖的資料，換了地圖要跟著刷新，
    // 不然會一直停在換地圖之前的舊資料
    initScenarioFromCurrentMap();
    initLlmgenFromCurrentMap();
    renderRunscriptPreview();
    renderBatchGroups();
    document.getElementById("mapSaveStatus").textContent = "已載入（" + screens.length + " 個畫面）";
  }

  // 「匯出目前地圖」的 zip（地圖＋截圖）匯入成專案裡的地圖；同名的話問要不要取代
  async function importMapZip(file) {
    var name = (prompt("匯入成專案「" + currentProject.name + "」的地圖，地圖名稱：\n\n" +
      "（zip 裡的截圖本機沒有的會一起補進專案；跟現有地圖同名的話，下一步會問要不要取代）",
      file.name.replace(/\.zip$/i, "")) || "").trim();
    if (!name) return;
    if (!(await flushMapSave()) && !confirm("地圖「" + currentMapName + "」還有修改沒存進去，匯入後會丟掉這些修改，確定嗎？")) return;
    var overwrite = false;
    for (;;) {
      var r = await postZip(mapApi(name) + "/import" + (overwrite ? "?overwrite=1" : ""), file);
      if (r.ok) {
        projectMaps = r.data.maps;
        mapDirty = false;
        await openMap(name, true);
        alert("已匯入地圖「" + name + "」" + (r.data.screenshots ? "，補進 " + r.data.screenshots + " 張截圖" : "") +
          (r.data.backup ? "\n原本的地圖已備份到 backups/" : ""));
        return;
      }
      if (r.status !== 409 || overwrite) { alert("匯入地圖失敗：" + (r.data.error || ("HTTP " + r.status))); return; }
      if (confirm("地圖「" + name + "」已經存在，要用匯入的取代嗎？\n（原本的會先備份到 backups/；按取消可以換一個名稱）")) {
        overwrite = true;
        continue;
      }
      name = (prompt("另存成新的地圖名稱（留空取消）：", name + " (匯入)") || "").trim();
      if (!name) return;
    }
  }

  function loadMapFromFile(file) {
    if (/\.zip$/i.test(file.name)) {
      if (!currentProject) { alert("地圖 zip（含截圖）要先開一個專案才能匯入，截圖會放進專案資料夾"); return; }
      importMapZip(file).catch(function (err) { alert("匯入地圖失敗：" + err.message); });
      return;
    }
    var reader = new FileReader();
    reader.onload = function () {
      var data;
      try { data = JSON.parse(reader.result); } catch (err) { alert("JSON 解析失敗：" + err.message); return; }
      if (!data.screens || !Array.isArray(data.screens)) { alert("格式不對，找不到 screens 陣列（要用這個工具自己匯出的檔案，或專案正式的 app_map_tree.json）。"); return; }
      if (currentProject) {
        // 開著專案時載入檔案＝匯入成專案裡的一張新地圖，不會蓋掉現有的。截圖不會跟著
        // 搬進專案，舊地圖連同截圖要搬進來請用 aitest-workspace import-legacy
        var suggested = file.name.replace(/\.json$/i, "").replace(/[^\w一-鿿 .()（）-]+/g, "_");
        var name = (prompt("匯入成專案「" + currentProject.name + "」的一張新地圖，地圖名稱：\n\n" +
          "注意：地圖用到的截圖不會一起搬進專案，畫面可能沒有截圖。舊地圖連同截圖搬進專案請用：\n" +
          "uv run aitest-workspace import-legacy " + currentProject.id + " --map " + file.name + " --map_name 名稱", suggested) || "").trim();
        if (!name) return;
        createProjectMap(name, data).catch(function (err) { alert("匯入地圖失敗：" + err.message); });
        return;
      }
      applyMapData(data);
    };
    reader.readAsText(file);
  }

  document.getElementById("importMapInput").addEventListener("change", function (e) {
    var file = e.target.files[0];
    if (!file) return;
    loadMapFromFile(file);
    e.target.value = "";
  });

  // ---- detail panel ----
  var detailBody = document.getElementById("detailBody");
  var detailCounter = document.getElementById("detailCounter");

  function edgeKindLabel(k) {
    return { tree: "tree", "cross-link": "cross-link", noop: "noop", external: "外部" }[k] || k;
  }

  function describeElement(el) {
    return el.n ? el.n : (el.l || el.c);
  }

  function renderBreadcrumb(s) {
    var parts = ["<b>" + escapeHtml(app.root_name || "root") + "</b>"];
    (s.path || []).forEach(function (step) {
      var stepScreen = byUid[step.screen_uid];
      var label = step.element_uid.split("::").pop();
      parts.push('<span class="sep">›</span>');
      parts.push('<span class="mono">' + escapeHtml(label) + "</span>");
    });
    if (s.path && s.path.length) {
      parts.push('<span class="sep">›</span><b>' + escapeHtml(s.name) + "</b>");
    }
    return parts.join(" ");
  }

  function escapeHtml(str) {
    return String(str == null ? "" : str).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // 專案地圖的截圖路徑相對於專案資料夾（screenshots/xxx.png），由伺服器的
  // /projects/<專案>/screenshots/ 提供；沒開專案時照舊相對於伺服器的工作目錄
  function screenshotUrl(s) {
    if (!s.screenshot) return "";
    var path = currentProject && !/^(\/|[a-z]+:)/i.test(s.screenshot)
      ? "projects/" + encodeURIComponent(currentProject.id) + "/" + s.screenshot : s.screenshot;
    return path + (s._scannedAt ? ("?v=" + s._scannedAt) : "");
  }

  function renderEdgeRow(e, direction) {
    var otherUid = direction === "out" ? e.to : e.from;
    var other = otherUid ? byUid[otherUid] : null;
    var viaLabel = e.via.split("::").pop();
    var kindClass = "kind-" + e.kind;
    var toLabel = direction === "out"
      ? (e.kind === "noop" ? "（原地）" : (other ? other.name : "App 外"))
      : (other ? other.name : "?");
    return '<div class="edge-row">' +
      '<span class="kind-tag ' + kindClass + '">' + edgeKindLabel(e.kind) + "</span>" +
      '<span class="via" title="' + escapeHtml(e.via) + '">' + escapeHtml(viaLabel) + "</span>" +
      '<span class="to">' + (direction === "out" ? "→ " : "← ") + escapeHtml(toLabel) + "</span>" +
      "</div>";
  }

  function renderElementTable(s, filterText, clickableOnly) {
    var rows = s.els.filter(function (el) {
      if (clickableOnly && !el.a) return false;
      if (!filterText) return true;
      var hay = (el.n || "") + " " + (el.l || "") + " " + (el.c || "") + " " + (el.d || "");
      return hay.toLowerCase().indexOf(filterText.toLowerCase()) !== -1;
    });
    var html = '<div class="table-scroll"><table class="el-table"><thead><tr>' +
      "<th>狀態</th><th>class</th><th>native_id／label</th><th>LLM 用途描述</th>" +
      "</tr></thead><tbody>";
    rows.forEach(function (el) {
      var st = elStatus[key(s.uid, el.u)];
      var dotClass = "dot-none", tag = "—";
      if (st) {
        if (st.type === "edge") { dotClass = "dot-" + st.kind; tag = edgeKindLabel(st.kind); }
        else { dotClass = "dot-chrome"; tag = "chrome"; }
      }
      html += "<tr>" +
        '<td class="cls"><span class="status-dot ' + dotClass + '"></span>' + tag + "</td>" +
        '<td class="cls">' + escapeHtml(shortClass(el.c)) + "</td>" +
        '<td class="lbl">' + escapeHtml(describeElement(el)) +
        (el.n && el.l ? '<span class="native">' + escapeHtml(el.l) + "</span>" : "") + "</td>" +
        '<td class="lbl">' + (el.d ? escapeHtml(el.d) : '<span style="color:var(--ink-soft);font-style:italic;">（沒有描述）</span>') + "</td>" +
        "</tr>";
    });
    html += "</tbody></table></div>";
    return html;
  }

  function renderBlankScreenDetail(s) {
    var html = "";
    html += '<div class="screen-id">';
    html += '<div class="name">' + escapeHtml(s.name) + ' <span style="color:var(--gold);font-weight:600;">（尚未掃描）</span></div>';
    html += '<div class="uid mono">uid ' + escapeHtml(s.uid) + "</div>";
    html += '<div class="breadcrumb">' + renderBreadcrumb(s) + "</div>";
    html += "</div>";
    html += '<div class="empty-hint" style="margin-top:10px;">這是手動新增的空白節點，剛才的點擊已經送到裝置——請先確認手機畫面已經停在這個新節點對應的畫面，再對「目前畫面」執行一次掃描。</div>';
    html += '<div class="field-row"><label><input type="checkbox" id="blankScanDescribe"> 同時產生LLM用途描述（會花費API額度，可以先不勾、之後再補）</label></div>';
    html += '<div class="field-row"><label><input type="checkbox" id="blankScanScroll"> 同時捲動收集完整清單（畫面有捲軸時可以抓到更多元件，但捲動後抓到的元件座標是相對「捲到那邊當下」的畫面，跟這裡顯示的截圖對不上、疊圖會顯示錯位／重疊，點擊定位也可能因此點錯地方——不確定要不要開就先不要勾）</label></div>';
    html += '<div class="btn-row"><button class="btn primary" id="blankScanBtn">🔍 掃描目前畫面</button><button class="btn danger" id="deleteNodeBtn">🗑 刪除節點</button></div>';
    html += '<div class="tap-result pending" id="blankScanResult">還沒掃描</div>';
    detailBody.innerHTML = html;
    document.getElementById("blankScanBtn").addEventListener("click", function () { runBlankScreenScan(s); });
    document.getElementById("deleteNodeBtn").addEventListener("click", function () { deleteNode(s.uid); });
  }

  function collectDescendantUids(rootUid) {
    var result = [rootUid];
    var queue = [rootUid];
    while (queue.length) {
      var cur = queue.shift();
      screens.forEach(function (s) {
        if (s.parent === cur) { result.push(s.uid); queue.push(s.uid); }
      });
    }
    return result;
  }

  function deleteNode(uid) {
    var s = byUid[uid];
    if (!s) return;
    var toDelete = collectDescendantUids(uid);
    var toDeleteSet = {};
    toDelete.forEach(function (u) { toDeleteSet[u] = true; });

    var confirmMsg = toDelete.length > 1
      ? "確定要刪除「" + s.name + "」嗎？這會一併刪除底下 " + (toDelete.length - 1) + " 個子節點，無法復原（除非你之前有匯出過地圖或專案）。"
      : "確定要刪除「" + s.name + "」嗎？無法復原（除非你之前有匯出過地圖或專案）。";
    if (!confirm(confirmMsg)) return;

    var parentUid = s.parent;
    screens = screens.filter(function (sc) { return !toDeleteSet[sc.uid]; });
    edges = edges.filter(function (e) { return !toDeleteSet[e.from] && !toDeleteSet[e.to]; });
    skipped = skipped.filter(function (sk) { return !toDeleteSet[sk.screen_uid]; });
    DATA.screens = screens; DATA.edges = edges; DATA.skipped = skipped;

    selectedUid = null;
    rebuildAll();
    markMapDirty();

    if (screens.length === 0) {
      detailCounter.textContent = "";
      renderEmptyMapDetail();
    } else if (parentUid && byUid[parentUid]) {
      selectScreen(parentUid);
    } else {
      var newRoot = screens.filter(function (sc) { return !sc.parent; })[0] || screens[0];
      selectScreen(newRoot.uid);
    }
  }

  function renderEmptyMapDetail() {
    var html = "";
    html += '<div class="empty-hint">目前地圖沒有任何畫面。輸入名稱、把手機導到你想當作起點的畫面，按下面按鈕建立主節點（會直接掃描App目前畫面）。</div>';
    html += '<div class="field-row" style="margin-top:10px;flex-direction:column;align-items:stretch;">';
    html += '<label style="font-size: calc(12px * var(--font-scale));color:var(--ink-soft);margin-bottom:4px;">主節點名稱</label>';
    html += '<input type="text" id="rootNodeName" value="root_screen" style="width:100%;padding:7px 10px;border-radius:8px;border:1px solid var(--line);background:var(--paper);color:var(--ink);font-size: calc(12.5px * var(--font-scale));">';
    html += "</div>";
    html += '<div class="btn-row" style="margin-top:8px;"><button class="btn primary" id="createRootBtn">🌱 建立主節點</button></div>';
    html += '<div class="empty-hint" style="margin-top:16px;">——或者，如果你手上已經有地圖 JSON（這個工具之前匯出的存檔，或專案正式的 app_map_tree.json）：</div>';
    html += '<div class="btn-row" style="margin-top:8px;"><label class="file-btn-label">📂 載入地圖 JSON<input type="file" id="emptyImportMapInput" accept="application/json"></label></div>';
    detailBody.innerHTML = html;
    document.getElementById("createRootBtn").addEventListener("click", function () {
      var name = (document.getElementById("rootNodeName").value || "").trim() || "root_screen";
      if (screens.length !== 0) { alert("地圖已經有節點了，不需要再建立主節點。"); return; }
      delete app.platform;
      var newUid = "root_" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
      var newScreen = {
        uid: newUid, name: name, parent: null, entry: null, depth: 0, path: [],
        nav_title: null, selected_tab: null, screenshot: null, w: null, h: null,
        els: [], _blank: true
      };
      if (currentProject) app.platform = currentProject.platform;
      screens.push(newScreen);
      rebuildAll();
      selectScreen(newUid);
      markMapDirty();
    });
    document.getElementById("emptyImportMapInput").addEventListener("change", function (e) {
      var file = e.target.files[0];
      if (!file) return;
      loadMapFromFile(file);
      e.target.value = "";
    });
  }

  function toShortEl(el) {
    var out = { u: el.unique_id, n: el.native_id, c: el.class, l: el.label, v: el.value, a: el.accessible != null ? el.accessible : el.clickable, d: el.description || "", src: el.id_source, b: el.bounds, fp: el.fingerprint };
    if (el.custom_xpath) out.custom_xpath = el.custom_xpath;  // 匯出再載入時保留人工自訂的 XPath
    return hideOrig(out, el);
  }
  // 掃描結果裡畫面層級的欄位（截圖保護、是否穩定、掃描時間）記到 _orig，存檔時才帶得出去
  function rememberScanMeta(s, screenData) {
    hideOrig(s, Object.assign({}, s._orig || {}, {
      last_scanned: screenData.last_scanned || null,
      screenshot_blocked: !!screenData.screenshot_blocked,
      screen_stable_at_scan: screenData.screen_stable_at_scan !== undefined ? screenData.screen_stable_at_scan : null
    }));
  }

  function rememberMapPlatform(screenData) {
    if (!app.platform && screenData.platform) app.platform = screenData.platform;
  }

  function applyScanResultToBlankScreen(s, screenData) {
    rememberMapPlatform(screenData);
    s.els = (screenData.elements || []).map(toShortEl);
    s.screenshot = screenData.screenshot || null;
    s.w = screenData.screen_width || null;
    s.h = screenData.screen_height || null;
    s._scannedAt = Date.now(); // 快取破壞用：同畫面重掃檔名不變，靠這個逼瀏覽器重新讀檔而不是用舊快取
    rememberScanMeta(s, screenData);
    delete s._blank;
  }

  async function runBlankScreenScan(s) {
    var btn = document.getElementById("blankScanBtn");
    var resultEl = document.getElementById("blankScanResult");
    var base = API_BASE;
    var describe = document.getElementById("blankScanDescribe").checked;
    var scroll = document.getElementById("blankScanScroll").checked;
    btn.disabled = true;
    resultEl.className = "tap-result pending";
    resultEl.textContent = "掃描中，元件多或畫面有可捲動內容時可能要等一段時間…";
    try {
      await assertPlatformMatches(base, mapPlatform());
      var res = await fetch(base + "/scan_screen", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ screen_name: s.name, describe: describe, scroll: scroll, project: currentProject ? currentProject.id : null })
      });
      var data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || ("HTTP " + res.status));
      applyScanResultToBlankScreen(s, data.screen_data);
      rebuildAll();
      selectScreen(s.uid);
      markMapDirty();
    } catch (err) {
      resultEl.textContent = failText("掃描失敗", err, "（viewer_server.py 有啟動嗎？手機是否已經停在這個新畫面？）");
      resultEl.className = "tap-result fail";
      btn.disabled = false;
    }
  }

  function selectScreen(uid) {
    selectedUid = uid;
    Object.keys(nodeGroups).forEach(function (u) {
      nodeGroups[u].classList.toggle("selected", u === uid);
    });
    var s = byUid[uid];
    if (s._blank) { renderBlankScreenDetail(s); return; }

    var outEdges = edgesByFrom[uid] || [];
    var inEdges = edgesByTo[uid] || [];
    var chromeSkips = skippedByScreen[uid] || [];

    detailCounter.textContent = (screens.indexOf(s) + 1) + " / " + screens.length;

    var html = "";
    html += '<div class="shot-row">';
    html += s.screenshot
      ? '<div class="shot-frame"><img src="' + escapeHtml(screenshotUrl(s)) + '" loading="lazy" alt="' + escapeHtml(s.name) + ' 截圖"></div>'
      : "";
    html += '<div class="screen-id">';
    html += '<div class="name">' + escapeHtml(s.name) + "</div>";
    html += '<div class="uid mono">uid ' + escapeHtml(s.uid) + "</div>";
    if (s.nav_title) html += '<div class="uid">nav_title：<span class="mono">' + escapeHtml(s.nav_title) + "</span></div>";
    html += '<div class="breadcrumb">' + renderBreadcrumb(s) + "</div>";
    html += "</div></div>";

    html += '<div class="mini-stats">';
    html += '<div class="mini-stat"><b>' + s.els.length + "</b>元件</div>";
    html += '<div class="mini-stat"><b>' + outEdges.length + "</b>已點候選</div>";
    html += '<div class="mini-stat"><b>' + chromeSkips.length + "</b>chrome 排除</div>";
    html += '<div class="mini-stat"><b>' + inEdges.length + "</b>可抵達路徑</div>";
    html += "</div>";

    html += '<div class="btn-row"><button class="btn" id="addNodeBtn" title="開啟視窗：在這個畫面選一個元件點擊，把點出來的畫面建成子節點（手動建樹）">🌱 從這裡點出新畫面…</button>' +
      (s.parent ? "" : '<button class="btn" id="addParentRootBtn" title="這個根節點上面還有一頁（例如 App 一打開的首頁）時，掃描那一頁當新的根節點，把現在這整棵樹接到它底下">⬆ 在上面加一層</button>') +
      '<button class="btn" id="describeScreenBtn" title="用存好的截圖和元件資料幫還沒有描述的元件產生用途描述，不需要接手機；元件 id、自訂 XPath 都不會變">🤖 補 LLM 描述</button>' +
      '<button class="btn danger" id="deleteNodeBtn">🗑 刪除節點（含所有子節點）</button></div>';
    html += '<div><span class="section-label">如何抵達這個畫面</span>';
    html += inEdges.length
      ? '<div class="edge-list">' + inEdges.map(function (e) { return renderEdgeRow(e, "in"); }).join("") + "</div>"
      : '<div class="empty-hint">根畫面，沒有上層路徑</div>';
    html += "</div>";

    html += '<div><span class="section-label">從這裡點出去的候選（' + outEdges.length + '）</span>';
    html += outEdges.length
      ? '<div class="edge-list">' + outEdges.map(function (e) { return renderEdgeRow(e, "out"); }).join("") + "</div>"
      : '<div class="empty-hint">這個畫面的候選還沒被點過（爬蟲提早結束）</div>';
    html += "</div>";

    html += '<div><span class="section-label">元件明細（' + s.els.length + '，可搜尋）</span>';
    html += '<input class="search-box" type="text" placeholder="搜尋 class / native_id / label…" id="elSearch">';
    html += '<label style="display:flex;align-items:center;gap:5px;font-size: calc(12px * var(--font-scale));color:var(--ink-soft);margin-top:6px;">' +
      '<input type="checkbox" id="elClickableOnly"> 只顯示可點擊元件（iOS accessible／Android clickable=true，跟畫面上實際可互動的元件大致相符，但不是100%精準）</label>';
    html += '<div id="elTableHost" style="margin-top:6px;">' + renderElementTable(s, "", false) + "</div>";
    html += "</div>";

    detailBody.innerHTML = html;
    function updateElTable() {
      var filterText = document.getElementById("elSearch").value;
      var clickableOnly = document.getElementById("elClickableOnly").checked;
      document.getElementById("elTableHost").innerHTML = renderElementTable(s, filterText, clickableOnly);
    }
    document.getElementById("elSearch").addEventListener("input", updateElTable);
    document.getElementById("elClickableOnly").addEventListener("change", updateElTable);
    var shotFrame = detailBody.querySelector(".shot-frame");
    if (shotFrame) shotFrame.addEventListener("click", function () { openModal(s); });
    document.getElementById("addNodeBtn").addEventListener("click", function () { openModal(s, { addNode: true }); });
    document.getElementById("deleteNodeBtn").addEventListener("click", function () { deleteNode(s.uid); });
    document.getElementById("describeScreenBtn").addEventListener("click", function () { describeScreen(s, this); });
    var addParentBtn = document.getElementById("addParentRootBtn");
    if (addParentBtn) addParentBtn.addEventListener("click", function () { renderAddParentRoot(s); });
  }

  // ---- 補 LLM 描述：只送還沒有描述的元件（都有了才問要不要全部重新產生）----
  async function describeScreen(s, btn) {
    var targets = s.els.filter(function (el) { return !el.d; });
    if (!targets.length) {
      if (!confirm("「" + s.name + "」的 " + s.els.length + " 個元件都已經有描述了，要全部重新產生嗎？（會覆蓋現有描述，花費 API 額度）")) return;
      targets = s.els.slice();
    } else if (!confirm("幫「" + s.name + "」" + targets.length + " 個還沒有描述的元件產生 LLM 用途描述？（會花費 API 額度）")) return;
    btn.disabled = true;
    btn.textContent = "產生描述中…";
    try {
      var full = shortScreenToFull(s);
      var ids = {};
      targets.forEach(function (el) { ids[el.u] = true; });
      var res = await fetch(API_BASE + "/describe_screen", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ screen_name: s.name, platform: mapPlatform(), project: currentProject ? currentProject.id : null,
          screenshot: s.screenshot, screenshot_blocked: full.screenshot_blocked,
          elements: full.elements.filter(function (e) { return ids[e.unique_id]; }) })
      });
      var data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || ("HTTP " + res.status));
      var filled = 0;
      s.els.forEach(function (el) {
        if (data.descriptions[el.u]) { el.d = data.descriptions[el.u]; filled++; }
      });
      if (filled) markMapDirty();
      selectScreen(s.uid);
      alert("已補上 " + filled + " / " + targets.length + " 個元件的描述" + (filled < targets.length ? "（沒補到的是 LLM 沒回傳，可以再按一次）" : ""));
    } catch (err) {
      alert(failText("產生描述失敗", err, "（viewer_server.py 有啟動嗎？）"));
      btn.disabled = false;
      btn.textContent = "🤖 補 LLM 描述";
    }
  }

  // ---- 在根節點上面加一層：掃描更上一層的畫面當新根，選它上面「點了進到原本根節點」的元件接起來 ----
  function renderAddParentRoot(oldRoot) {
    var suggested = "home_screen";
    for (var i = 2; screens.some(function (sc) { return sc.name === suggested; }); i++) suggested = "home_screen_" + i;
    detailBody.innerHTML =
      '<div class="screen-id"><div class="name">在「' + escapeHtml(oldRoot.name) + '」上面加一層</div></div>' +
      '<div class="empty-hint" style="margin-top:10px;">1. 把手機導到「' + escapeHtml(oldRoot.name) + '」的上一層那頁（例如 App 一打開的首頁），輸入名稱後掃描。<br>' +
      "2. 接著在新畫面上選「點了會進到「" + escapeHtml(oldRoot.name) + "」」的元件，會真的點一次，確認手機到了原本的根節點才接起來。<br>" +
      "原本的節點、測案都不受影響，只是整棵樹往下一層。</div>" +
      '<div class="field-row" style="margin-top:10px;flex-direction:column;align-items:stretch;">' +
      '<label style="font-size: calc(12px * var(--font-scale));color:var(--ink-soft);margin-bottom:4px;">新根節點名稱</label>' +
      '<input type="text" id="parentRootName" value="' + escapeHtml(suggested) + '" style="width:100%;padding:7px 10px;border-radius:8px;border:1px solid var(--line);background:var(--paper);color:var(--ink);font-size: calc(12.5px * var(--font-scale));">' +
      "</div>" +
      '<div class="field-row"><label><input type="checkbox" id="parentRootDescribe"> 同時產生LLM用途描述（會花費API額度，可以先不勾、之後再補）</label></div>' +
      '<div class="btn-row" style="margin-top:8px;"><button class="btn primary" id="parentRootScanBtn">🔍 掃描目前畫面</button><button class="btn" id="parentRootCancelBtn">取消</button></div>' +
      '<div class="tap-result pending" id="parentRootResult">還沒掃描</div>';
    document.getElementById("parentRootCancelBtn").addEventListener("click", function () { selectScreen(oldRoot.uid); });
    document.getElementById("parentRootScanBtn").addEventListener("click", async function () {
      var btn = this, resultEl = document.getElementById("parentRootResult");
      var name = (document.getElementById("parentRootName").value || "").trim();
      if (!name) { resultEl.textContent = "請先輸入名稱"; resultEl.className = "tap-result fail"; return; }
      if (screens.some(function (sc) { return sc.name === name; })) { resultEl.textContent = "這個名稱已經有節點在用了，換一個名稱"; resultEl.className = "tap-result fail"; return; }
      btn.disabled = true;
      resultEl.className = "tap-result pending";
      resultEl.textContent = "掃描中，勾了 LLM 描述的話要等久一點…";
      try {
        await assertPlatformMatches(API_BASE, mapPlatform());
        var res = await fetch(API_BASE + "/scan_screen", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ screen_name: name, describe: document.getElementById("parentRootDescribe").checked, scroll: false,
            project: currentProject ? currentProject.id : null })
        });
        var data = await res.json();
        if (!res.ok || data.error) throw new Error(data.error || ("HTTP " + res.status));
        // 先不放進地圖：選好入口、確認點得到原本的根節點才接上，取消的話地圖完全不變
        var newRoot = {
          uid: "manual_" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6), name: name,
          parent: null, entry: null, depth: 0, path: [], nav_title: null, selected_tab: null,
          screenshot: null, w: null, h: null, els: [], _blank: true
        };
        applyScanResultToBlankScreen(newRoot, data.screen_data);
        resultEl.textContent = "掃描好了，在跳出的視窗選入口元件（關掉視窗就是取消，地圖不會變）";
        resultEl.className = "tap-result pass";
        btn.disabled = false;
        openModal(newRoot, { linkRoot: oldRoot });
      } catch (err) {
        resultEl.textContent = failText("掃描失敗", err, "（viewer_server.py 有啟動嗎？）");
        resultEl.className = "tap-result fail";
        btn.disabled = false;
      }
    });
  }

  function renderModalPickedLinkRoot(el, host) {
    var oldRoot = modalLinkRoot;
    host.innerHTML =
      '<div class="picked-panel">' +
      '<div class="pk-row"><b>class</b><span class="mono">' + escapeHtml(shortClass(el.c)) + "</span></div>" +
      '<div class="pk-row"><b>native_id</b><span class="mono">' + escapeHtml(el.n || "—") + "</span></div>" +
      '<div class="pk-row"><b>描述</b><span>' + escapeHtml(el.d || "—") + "</span></div>" +
      '<div class="btn-row" style="margin-top:8px;"><button class="btn primary" id="linkRootSubmitBtn">⬇ 執行點擊並接上「' + escapeHtml(oldRoot.name) + "」</button></div>" +
      '<div class="tap-result pending" id="linkRootResult">還沒送出</div>' +
      "</div>";
    document.getElementById("linkRootSubmitBtn").addEventListener("click", function () { submitLinkRoot(el); });
  }

  async function submitLinkRoot(el) {
    var btn = document.getElementById("linkRootSubmitBtn");
    var resultEl = document.getElementById("linkRootResult");
    var newRoot = modalScreen, oldRoot = modalLinkRoot;
    btn.disabled = true;
    resultEl.className = "tap-result pending";
    resultEl.textContent = "執行點擊中…";
    try {
      await assertPlatformMatches(API_BASE, mapPlatform());
      var res = await fetch(API_BASE + "/tap", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(toFullEl(el)) });
      var data = await res.json();
      if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
      if (String(data.result).indexOf("PASS") !== 0) {
        resultEl.textContent = "點擊失敗，還沒接上：" + data.result;
        resultEl.className = "tap-result fail";
        btn.disabled = false;
        return;
      }
      resultEl.textContent = "已點擊，確認手機是不是到了「" + oldRoot.name + "」…";
      await new Promise(function (r) { setTimeout(r, 1500); });  // 等換頁動畫
      var check = await (await fetch(API_BASE + "/check_screen", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ screen_name: oldRoot.name, elements: shortScreenToFull(oldRoot).elements })
      })).json();
      if (check.error) throw new Error(check.error);
      if (check.status === "fail") {
        resultEl.innerHTML = escapeHtml("手機看起來沒有到「" + oldRoot.name + "」：" + check.message +
          "。請把手機回到「" + newRoot.name + "」那頁再選一次；確定是對的（例如畫面有彈窗擋住）也可以直接接上。") +
          '<div class="btn-row" style="margin-top:6px;"><button class="btn" id="linkRootForceBtn">仍然接上</button></div>';
        resultEl.className = "tap-result fail";
        document.getElementById("linkRootForceBtn").addEventListener("click", function () { linkNewRoot(newRoot, oldRoot, el); });
        btn.disabled = false;
        return;
      }
      linkNewRoot(newRoot, oldRoot, el);
    } catch (err) {
      resultEl.textContent = failText("連線失敗", err, "（viewer_server.py 有啟動嗎？）");
      resultEl.className = "tap-result fail";
      btn.disabled = false;
    }
  }

  // 新根放最前面，原本的根接到它底下；原本整棵樹 depth +1、path 前面補「新根 → 原本的根」這一步
  function linkNewRoot(newRoot, oldRoot, el) {
    var step = { screen_uid: newRoot.uid, element_uid: el.u };
    collectDescendantUids(oldRoot.uid).forEach(function (uid) {
      var sc = byUid[uid];
      sc.depth = (sc.depth || 0) + 1;
      sc.path = [step].concat(sc.path || []);
    });
    oldRoot.parent = newRoot.uid;
    oldRoot.entry = el.u;
    screens.unshift(newRoot);
    edges.push({ from: newRoot.uid, via: el.u, to: oldRoot.uid, kind: "tree" });
    app.root_name = newRoot.name;
    if (typeof app.max_depth === "number") app.max_depth += 1;
    rebuildAll();
    markMapDirty();
    closeModal();
    selectScreen(newRoot.uid);
  }

  // ---------------------------------------------------------------
  // 放大操作視窗：大圖＋可點擊定位框＋元件清單＋送出點擊到裝置
  // ---------------------------------------------------------------
  var modal = document.getElementById("imgModal");
  var modalScreen = null;
  var modalPickedUid = null;
  var modalAddNode = false;
  var modalLinkRoot = null;  // 「在上面加一層」：要接到新根底下的原本根節點

  function openModal(s, opts) {
    opts = opts || {};
    modalScreen = s;
    modalPickedUid = null;
    modalAddNode = !!opts.addNode;
    modalLinkRoot = opts.linkRoot || null;
    document.getElementById("modalTitle").textContent = modalLinkRoot
      ? (s.name + " — 選一個點了會進到「" + modalLinkRoot.name + "」的元件，送出後會真的點擊裝置")
      : modalAddNode
      ? (s.name + " — 選一個元件當新節點的入口，送出後會真的點擊裝置")
      : (s.name + " — 點圖上的框或右邊清單來選元件");
    var img = document.getElementById("modalImg");
    img.src = screenshotUrl(s);
    img.alt = s.name;
    document.getElementById("modalClickableOnly").checked = true;
    var draw = function () { renderModalBoxes(); renderModalList("", true); };
    if (img.complete) draw(); else img.onload = draw;
    document.getElementById("modalSearch").value = "";
    document.getElementById("modalPickedHost").innerHTML = "";
    modal.hidden = false;
  }
  function closeModal() { modal.hidden = true; modalScreen = null; modalAddNode = false; modalLinkRoot = null; }
  document.getElementById("modalCloseBtn").addEventListener("click", closeModal);
  modal.addEventListener("click", function (e) { if (e.target === modal) closeModal(); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && !modal.hidden) closeModal(); });

  function pickModalElement(el, fromBox) {
    modalPickedUid = el.u;
    renderModalBoxes();
    renderModalList(document.getElementById("modalSearch").value, document.getElementById("modalClickableOnly").checked);
    renderModalPicked(el);
    // 從圖上點的，右邊清單捲到對應那一列，不用自己往下找橘紅色那筆
    if (fromBox) {
      var row = document.querySelector("#modalListHost .modal-list-row.picked");
      if (row) row.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }

  function renderModalBoxes() {
    var wrap = document.getElementById("modalStageWrap");
    wrap.querySelectorAll(".el-box").forEach(function (b) { b.remove(); });
    if (!modalScreen.w || !modalScreen.h) return;
    var clickableOnly = document.getElementById("modalClickableOnly").checked;
    var screenArea = modalScreen.w * modalScreen.h;
    modalScreen.els.forEach(function (el) {
      if (!el.b) return;
      if (clickableOnly && !el.a) return;
      var areaFrac = (el.b.width * el.b.height) / screenArea;
      if (areaFrac > 0.45) return; // 排除滿版容器，不然整片蓋住截圖
      var box = document.createElement("div");
      box.className = "el-box " + (el.src === "native" ? "native" : "generated") + (el.u === modalPickedUid ? " picked" : "");
      box.style.left = (el.b.x / modalScreen.w * 100) + "%";
      box.style.top = (el.b.y / modalScreen.h * 100) + "%";
      box.style.width = (el.b.width / modalScreen.w * 100) + "%";
      box.style.height = (el.b.height / modalScreen.h * 100) + "%";
      box.title = el.d || el.n || el.l || el.c;
      box.addEventListener("click", function () { pickModalElement(el, true); });
      wrap.appendChild(box);
    });
  }

  function renderModalList(filterText, clickableOnly) {
    var host = document.getElementById("modalListHost");
    var rows = modalScreen.els.filter(function (el) {
      if (clickableOnly && !el.a) return false;
      if (!filterText) return true;
      var hay = (el.n || "") + " " + (el.l || "") + " " + (el.c || "") + " " + (el.d || "");
      return hay.toLowerCase().indexOf(filterText.toLowerCase()) !== -1;
    });
    host.innerHTML = "";
    rows.forEach(function (el) {
      var row = document.createElement("div");
      row.className = "modal-list-row" + (el.u === modalPickedUid ? " picked" : "");
      row.innerHTML =
        '<div class="row-top">' +
        '<span>' + escapeHtml(shortClass(el.c)) + "</span>" +
        (el.n ? "<span>· " + escapeHtml(el.n) + "</span>" : "") +
        "</div>" +
        '<div class="row-desc">' + escapeHtml(el.d || el.l || "（無描述）") + "</div>";
      row.addEventListener("click", function () { pickModalElement(el); });
      host.appendChild(row);
    });
    if (!rows.length) host.innerHTML = '<div class="empty-hint">沒有符合的元件</div>';
  }

  function updateModalList() {
    renderModalList(document.getElementById("modalSearch").value, document.getElementById("modalClickableOnly").checked);
  }
  document.getElementById("modalSearch").addEventListener("input", updateModalList);
  document.getElementById("modalClickableOnly").addEventListener("change", function () {
    renderModalBoxes();
    updateModalList();
  });

  // ---- XPath 編輯器 + AI定位（整合自 viewer.html）----
  function xpathLiteral(value) {
    value = value || "";
    if (value.indexOf('"') === -1) return '"' + value + '"';
    if (value.indexOf("'") === -1) return "'" + value + "'";
    var parts = value.split('"');
    return "concat(" + parts.map(function (p) { return '"' + p + '"'; }).join(", '\"', ") + ")";
  }
  function toFullEl(el) {
    var out = { unique_id: el.u, id_source: el.src, native_id: el.n, class: el.c, label: el.l, value: el.v, bounds: el.b };
    if (el.fp) out.fingerprint = el.fp;
    if (el.custom_xpath) out.custom_xpath = el.custom_xpath;
    return out;
  }
  // 比照 run_test_case.py（platforms/ios.py、platforms/android.py）實際執行時組的查詢：
  // iOS 查 @label；Android 沒有 label 屬性，text 跟 content-desc 都要試
  function textCondition(label) {
    var literal = xpathLiteral(label);
    return mapPlatform() === "android" ? "@text=" + literal + " or @content-desc=" + literal : "@label=" + literal;
  }
  function computeLocatorShort(el) {
    if (el.custom_xpath) return { type: "xpath", custom: true, text: el.custom_xpath, rawXPath: el.custom_xpath };
    if (el.src === "native") return { type: "id", text: (mapPlatform() === "android" ? "resource-id: " : "accessibility id: ") + el.n };
    var label = el.l || "";
    if (!label) return { type: "coordinate", text: "label為空，無法組出XPath，執行時會直接退回座標點擊備援" };
    var xpath = "//" + el.c + "[" + textCondition(label) + "]";
    return { type: "xpath", text: xpath, rawXPath: xpath };
  }
  function buildAncestorTemplateShort(el, ancestor) {
    var targetCond = textCondition(el.l || "");
    if (ancestor.resource_id) return '//*[@resource-id="' + ancestor.resource_id + '"]//' + el.c + "[" + targetCond + "]";
    if (ancestor.name) return '//*[@name="' + ancestor.name + '"]//' + el.c + "[" + targetCond + "]";
    return "//" + ancestor.class + "//" + el.c + "[" + targetCond + "]";
  }
  async function loadAncestorsShort(el, selectEl, resultEl) {
    resultEl.textContent = "讀取中..."; resultEl.className = "xpath-editor-result";
    try {
      var base = API_BASE;
      await assertPlatformMatches(base, mapPlatform());
      var res = await fetch(base + "/inspect", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(toFullEl(el)) });
      var data = await res.json();
      if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
      if (!data.found || !(data.ancestors || []).length) {
        resultEl.textContent = "在目前畫面上找不到這個元件的線索，請確認手機是否已導航到正確畫面";
        resultEl.className = "xpath-editor-result error";
        return;
      }
      selectEl.innerHTML = '<option value="">-- 選擇上層元素套用建議查詢 --</option>';
      data.ancestors.forEach(function (a) {
        var opt = document.createElement("option");
        opt.value = JSON.stringify(a);
        // iOS 回傳 name/label，Android 回傳 resource_id/text/content_desc
        var text = a.label || a.text || a.content_desc;
        var idPart = a.resource_id ? (" resource-id=" + a.resource_id)
          : a.name ? (" name=" + a.name)
          : text ? (' text="' + text + '"') : "";
        opt.textContent = "第" + a.level + "層 " + shortClass(a.class) + idPart;
        selectEl.appendChild(opt);
      });
      resultEl.textContent = "已載入 " + data.ancestors.length + " 層上層元素，選一層會自動帶入建議查詢";
      resultEl.className = "xpath-editor-result";
    } catch (err) {
      resultEl.textContent = failText("連線失敗", err, "（viewer_server.py 有啟動嗎？）");
      resultEl.className = "xpath-editor-result error";
    }
  }
  async function testXPathValueShort(xpath, resultEl) {
    if (!xpath) { resultEl.textContent = "請先輸入 XPath"; resultEl.className = "xpath-editor-result error"; return; }
    resultEl.textContent = "測試中..."; resultEl.className = "xpath-editor-result";
    try {
      var base = API_BASE;
      await assertPlatformMatches(base, mapPlatform());
      var res = await fetch(base + "/test_xpath", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ xpath: xpath }) });
      var data = await res.json();
      if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
      if (data.error) { resultEl.textContent = "XPath 語法錯誤：" + data.error; resultEl.className = "xpath-editor-result error"; return; }
      var suffix = data.count === 1 ? "（唯一，可以儲存）" : data.count === 0 ? "（查無結果）" : "（有歧義，不建議儲存）";
      resultEl.textContent = "查到 " + data.count + " 個結果" + suffix;
      resultEl.className = "xpath-editor-result " + (data.count === 1 ? "pass" : "fail");
    } catch (err) {
      resultEl.textContent = failText("連線失敗", err, "（viewer_server.py 有啟動嗎？）");
      resultEl.className = "xpath-editor-result error";
    }
  }
  async function callAgentXPathShort(el, resultEl, onApplied) {
    resultEl.textContent = "AI生成中，可能需要幾次嘗試，請稍候..."; resultEl.className = "xpath-editor-result";
    try {
      var base = API_BASE;
      await assertPlatformMatches(base, mapPlatform());
      var res = await fetch(base + "/agent_xpath", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(toFullEl(el)) });
      var data = await res.json();
      if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
      if (data.success) {
        el.custom_xpath = data.xpath;
        markMapDirty();
        resultEl.textContent = "AI已找到唯一XPath並套用（第" + data.attempts + "次嘗試成功）";
        resultEl.className = "xpath-editor-result pass";
        if (onApplied) onApplied(data.xpath);
      } else {
        resultEl.textContent = "AI無法定位出唯一XPath（已嘗試" + data.attempts + "次，最後一次：" +
          ((data.log && data.log[data.log.length - 1]) || "無詳細訊息") + "）";
        resultEl.className = "xpath-editor-result error";
      }
    } catch (err) {
      resultEl.textContent = failText("連線失敗", err, "（GEMINI_API_KEY 有設定嗎？）");
      resultEl.className = "xpath-editor-result error";
    }
  }

  function renderModalPickedAddNode(el, host) {
    // Android 的 native_id 前面帶整串套件名稱（com.xxx:id/tvBuy），只取 :id/ 後面那段；
    // 超過 40 字時從父名稱裁、保留元件那段，不然每一層截出來都一樣，一直撞名
    var clean = function (s) { return s.replace(/[^\w一-鿿]+/g, "_"); };
    var elPart = clean(((el.n || "").split(/:id\/|\//).pop()) || el.l || shortClass(el.c) || "child").slice(0, 30);
    var parentPart = clean(modalScreen.name).slice(0, Math.max(0, 40 - elPart.length - 1));
    var baseName = (parentPart ? parentPart + "_" : "") + elPart;
    var suggestedName = baseName;
    for (var i = 2; screens.some(function (s) { return s.name === suggestedName; }); i++) suggestedName = baseName + "_" + i;
    host.innerHTML =
      '<div class="picked-panel">' +
      '<div class="pk-row"><b>class</b><span class="mono">' + escapeHtml(shortClass(el.c)) + "</span></div>" +
      '<div class="pk-row"><b>native_id</b><span class="mono">' + escapeHtml(el.n || "—") + "</span></div>" +
      '<div class="pk-row"><b>描述</b><span>' + escapeHtml(el.d || "—") + "</span></div>" +
      '<div class="field-row" style="margin-top:8px;flex-direction:column;align-items:stretch;">' +
      '<label style="font-size: calc(12px * var(--font-scale));color:var(--ink-soft);margin-bottom:4px;">新畫面名稱</label>' +
      '<input type="text" id="addNodeScreenName" value="' + escapeHtml(suggestedName) + '" style="width:100%;padding:7px 10px;border-radius:8px;border:1px solid var(--line);background:var(--paper);color:var(--ink);font-size: calc(12.5px * var(--font-scale));">' +
      "</div>" +
      '<div class="btn-row" style="margin-top:8px;"><button class="btn primary" id="addNodeSubmitBtn">🌱 執行點擊並建立節點</button></div>' +
      '<div class="tap-result pending" id="addNodeResult">還沒送出</div>' +
      "</div>";
    document.getElementById("addNodeSubmitBtn").addEventListener("click", function () { submitAddNode(el); });
  }

  async function submitAddNode(el) {
    var nameInput = document.getElementById("addNodeScreenName");
    var btn = document.getElementById("addNodeSubmitBtn");
    var resultEl = document.getElementById("addNodeResult");
    var newName = (nameInput.value || "").trim();
    if (!newName) { resultEl.textContent = "請先輸入新畫面名稱"; resultEl.className = "tap-result fail"; return; }
    if (screens.some(function (s) { return s.name === newName; })) {
      resultEl.textContent = "這個名稱已經有節點在用了，換一個名稱";
      resultEl.className = "tap-result fail";
      return;
    }
    btn.disabled = true;
    resultEl.className = "tap-result pending";
    resultEl.textContent = "執行點擊中…";
    var base = API_BASE;
    var payload = toFullEl(el);
    try {
      await assertPlatformMatches(base, mapPlatform());
      var res = await fetch(base + "/tap", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      var data = await res.json();
      if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
      if (String(data.result).indexOf("PASS") !== 0) {
        resultEl.textContent = "點擊失敗，沒有建立節點：" + data.result;
        resultEl.className = "tap-result fail";
        btn.disabled = false;
        return;
      }
      var parentScreen = modalScreen;
      var newUid = "manual_" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
      var newScreen = {
        uid: newUid, name: newName, parent: parentScreen.uid, entry: el.u,
        depth: (parentScreen.depth || 0) + 1,
        path: (parentScreen.path || []).concat([{ screen_uid: parentScreen.uid, element_uid: el.u }]),
        nav_title: null, selected_tab: null, screenshot: null, w: null, h: null,
        els: [], _blank: true
      };
      screens.push(newScreen);
      edges.push({ from: parentScreen.uid, via: el.u, to: newUid, kind: "tree" });
      resultEl.textContent = "PASS，已建立新節點「" + newName + "」";
      resultEl.className = "tap-result pass";
      rebuildAll();
      markMapDirty();
      closeModal();
      selectScreen(newUid);
    } catch (err) {
      resultEl.textContent = failText("連線失敗", err, "（viewer_server.py 有啟動嗎？）");
      resultEl.className = "tap-result fail";
      btn.disabled = false;
    }
  }

  function renderModalPicked(el) {
    var host = document.getElementById("modalPickedHost");
    if (modalLinkRoot) { renderModalPickedLinkRoot(el, host); return; }
    if (modalAddNode) { renderModalPickedAddNode(el, host); return; }
    var locator = computeLocatorShort(el);
    var locatorTypeLabel = locator.custom ? "XPath(自訂)" : { id: "id", xpath: "XPath", coordinate: "座標" }[locator.type];
    host.innerHTML =
      '<div class="picked-panel">' +
      '<div class="pk-row"><b>class</b><span class="mono">' + escapeHtml(shortClass(el.c)) + "</span></div>" +
      '<div class="pk-row"><b>native_id</b><span class="mono">' + escapeHtml(el.n || "—") + "</span></div>" +
      '<div class="pk-row"><b>描述</b><span>' + escapeHtml(el.d || "—") + "</span></div>" +
      '<div class="el-locator' + (locator.custom ? " custom" : "") + '"><span class="locator-type">' + locatorTypeLabel + "</span>" + escapeHtml(locator.text) + "</div>" +
      '<div class="btn-row"><button class="btn primary" id="modalTapBtn">📲 送出點擊到裝置</button></div>' +
      '<div class="tap-result pending" id="modalTapResult">還沒送出</div>' +
      '<div class="btn-row"><button class="btn" id="modalXpathToggle">✏️ 重新定義XPath／🤖 AI定位</button></div>' +
      '<div id="modalXpathEditor" hidden>' +
      '<select class="plain-select" id="modalAncestorSelect" style="width:100%;margin-bottom:6px;"><option value="">-- 先載入上層元素 --</option></select>' +
      '<textarea class="xpath-input" id="modalXpathInput">' + escapeHtml(locator.rawXPath || "") + "</textarea>" +
      '<div class="btn-row" style="margin-top:6px;">' +
      '<button class="btn" id="modalLoadAncestorsBtn">載入上層元素</button>' +
      '<button class="btn" id="modalTestXpathBtn">測試</button>' +
      '<button class="btn primary" id="modalSaveXpathBtn">儲存</button>' +
      '<button class="btn" id="modalClearXpathBtn">清除自訂</button>' +
      '<button class="btn" id="modalAgentXpathBtn">🤖 AI定位</button>' +
      "</div>" +
      '<div class="xpath-editor-result" id="modalXpathResult" style="margin-top:6px;">尚未測試</div>' +
      "</div>" +
      "</div>";
    document.getElementById("modalTapBtn").addEventListener("click", function () { sendModalTap(el); });

    var editorHost = document.getElementById("modalXpathEditor");
    document.getElementById("modalXpathToggle").addEventListener("click", function () { editorHost.hidden = !editorHost.hidden; });
    var ancestorSelect = document.getElementById("modalAncestorSelect");
    var xpathInput = document.getElementById("modalXpathInput");
    var xpathResult = document.getElementById("modalXpathResult");
    document.getElementById("modalLoadAncestorsBtn").addEventListener("click", function () { loadAncestorsShort(el, ancestorSelect, xpathResult); });
    ancestorSelect.addEventListener("change", function () {
      if (!ancestorSelect.value) return;
      xpathInput.value = buildAncestorTemplateShort(el, JSON.parse(ancestorSelect.value));
    });
    document.getElementById("modalTestXpathBtn").addEventListener("click", function () { testXPathValueShort(xpathInput.value.trim(), xpathResult); });
    document.getElementById("modalSaveXpathBtn").addEventListener("click", function () {
      var value = xpathInput.value.trim();
      if (!value) { xpathResult.textContent = "請先輸入 XPath 再儲存"; xpathResult.className = "xpath-editor-result error"; return; }
      el.custom_xpath = value;
      markMapDirty();
      renderModalPicked(el);
    });
    document.getElementById("modalClearXpathBtn").addEventListener("click", function () {
      delete el.custom_xpath;
      markMapDirty();
      renderModalPicked(el);
    });
    document.getElementById("modalAgentXpathBtn").addEventListener("click", function () {
      callAgentXPathShort(el, xpathResult, function () { renderModalPicked(el); });
    });
  }

  async function sendModalTap(el) {
    var resultEl = document.getElementById("modalTapResult");
    var btn = document.getElementById("modalTapBtn");
    btn.disabled = true;
    resultEl.className = "tap-result pending";
    resultEl.textContent = "執行中…";
    var base = API_BASE;
    var payload = toFullEl(el);
    try {
      await assertPlatformMatches(base, mapPlatform());
      var res = await fetch(base + "/tap", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      var data = await res.json();
      if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
      resultEl.textContent = data.result;
      resultEl.className = "tap-result " + (String(data.result).indexOf("PASS") === 0 ? "pass" : "fail");
    } catch (err) {
      resultEl.textContent = failText("連線失敗", err, "（viewer_server.py 有啟動嗎？）");
      resultEl.className = "tap-result fail";
    } finally {
      btn.disabled = false;
    }
  }

  document.getElementById("modalServerCheckBtn").addEventListener("click", async function () {
    var base = API_BASE;
    await renderServerStatus(base, document.getElementById("modalServerStatus"), mapPlatform());
  });

  // ================= 分頁切換 =================
  document.querySelectorAll(".tab-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      document.querySelectorAll(".tab-btn").forEach(function (b) { b.classList.toggle("active", b === btn); });
      var target = btn.dataset.tab;
      document.querySelectorAll(".tab-panel").forEach(function (p) { p.classList.toggle("active", p.id === "tab-" + target); });
      syncTabsWithCurrentMap();
    });
  });

  // 地圖檢視裡新增／刪除／掃描節點都是直接改 screens，不會通知其他分頁；
  // 切換分頁時重新抓一次目前地圖，其他分頁才看得到剛建好的節點。已勾選的畫面依名稱保留。
  function syncTabsWithCurrentMap() {
    rebuildKeepingChecks(scenarioScreenChecklist, "scenario-screen-cb", function () { return scenarioData; },
      initScenarioFromCurrentMap, updateScenarioGenerateEnabled);
    rebuildKeepingChecks(llmgenScreenChecklist, "llmgen-screen-cb", function () { return llmgenData; },
      initLlmgenFromCurrentMap, updateLlmgenSendEnabled);
    // 測案執行在執行當下才抓目前地圖，這裡只更新來源標示，不重畫預覽以免洗掉執行結果
    renderRunscriptSources();
  }
  function rebuildKeepingChecks(container, cbClass, getData, reinit, onChange) {
    var prevChecked = Object.create(null);
    var oldData = getData();
    container.querySelectorAll("." + cbClass).forEach(function (cb) {
      var s = oldData && oldData.screens[Number(cb.value)];
      if (s) prevChecked[s.screen_name] = cb.checked;
    });
    reinit();
    var newData = getData();
    container.querySelectorAll("." + cbClass).forEach(function (cb) {
      var name = newData.screens[Number(cb.value)].screen_name;
      if (name in prevChecked) cb.checked = prevChecked[name];
    });
    onChange();
  }

  // ================= 測試情境產生 =================
  // 目的：先看過選定畫面的資料，推論出一批「值得測試的情境」描述清單，
  // 使用者從清單挑一個情境，直接送去下面的「LLM對話式產生跨畫面測案」，
  // 由那邊根據情境描述產生真正逐步驟的測試案例——這裡只做「情境發想」，
  // 不直接產生測試步驟。
  var scenarioScreenChecklist = document.getElementById("scenarioScreenChecklist");
  var scenarioScreensCount = document.getElementById("scenarioScreensCount");
  var scenarioSelectAllBtn = document.getElementById("scenarioSelectAllBtn");
  var scenarioSelectNoneBtn = document.getElementById("scenarioSelectNoneBtn");
  var scenarioGenerateBtn = document.getElementById("scenarioGenerateBtn");
  var scenarioResultHost = document.getElementById("scenarioResultHost");
  var scenarioViewPromptBtn = document.getElementById("scenarioViewPromptBtn");
  var scenarioPromptPanel = document.getElementById("scenarioPromptPanel");
  var scenarioPromptInput = document.getElementById("scenarioPromptInput");
  var scenarioPromptResetBtn = document.getElementById("scenarioPromptResetBtn");
  var scenarioData = null, scenarioLastList = null;

  async function fetchScenarioDefaultPrompt() {
    var base = API_BASE;
    var res = await fetch(base + "/llm_scenarios_prompt");
    var data = await res.json();
    if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
    return data.template;
  }
  scenarioViewPromptBtn.addEventListener("click", async function () {
    scenarioPromptPanel.hidden = !scenarioPromptPanel.hidden;
    if (!scenarioPromptPanel.hidden && !scenarioPromptInput.value) {
      scenarioPromptInput.value = "讀取中...";
      try {
        scenarioPromptInput.value = await fetchScenarioDefaultPrompt();
      } catch (err) {
        scenarioPromptInput.value = "";
        alert("讀取提示詞失敗：" + err.message + "（viewer_server.py 有啟動嗎？）");
      }
    }
  });
  scenarioPromptResetBtn.addEventListener("click", async function () {
    scenarioPromptResetBtn.disabled = true;
    try {
      scenarioPromptInput.value = await fetchScenarioDefaultPrompt();
    } catch (err) {
      alert("讀取提示詞失敗：" + err.message);
    } finally {
      scenarioPromptResetBtn.disabled = false;
    }
  });

  function initScenarioFromCurrentMap() {
    scenarioData = { screens: shortScreensToLlmgenFormat(screens) };
    buildScenarioScreenChecklist();
  }
  function buildScenarioScreenChecklist() {
    scenarioScreenChecklist.innerHTML = "";
    (scenarioData.screens || []).forEach(function (screen, idx) {
      var label = document.createElement("label");
      label.className = "filter-chip";
      label.innerHTML = '<input type="checkbox" class="scenario-screen-cb" value="' + idx + '" checked /> ' +
        escapeHtml(screen.screen_name) + "（" + (screen.elements || []).length + "）";
      scenarioScreenChecklist.appendChild(label);
    });
    scenarioScreenChecklist.querySelectorAll(".scenario-screen-cb").forEach(function (cb) { cb.addEventListener("change", updateScenarioGenerateEnabled); });
    updateScenarioGenerateEnabled();
  }
  function scenarioSelectedScreens() {
    var idxs = Array.prototype.slice.call(scenarioScreenChecklist.querySelectorAll(".scenario-screen-cb:checked")).map(function (cb) { return Number(cb.value); });
    return idxs.map(function (i) { return scenarioData.screens[i]; });
  }
  function updateScenarioGenerateEnabled() {
    var selectedCount = scenarioSelectedScreens().length;
    var totalCount = (scenarioData && scenarioData.screens || []).length;
    scenarioScreensCount.textContent = "已選 " + selectedCount + " / " + totalCount;
    scenarioGenerateBtn.disabled = selectedCount === 0;
  }
  scenarioSelectAllBtn.addEventListener("click", function () {
    scenarioScreenChecklist.querySelectorAll(".scenario-screen-cb").forEach(function (cb) { cb.checked = true; });
    updateScenarioGenerateEnabled();
  });
  scenarioSelectNoneBtn.addEventListener("click", function () {
    scenarioScreenChecklist.querySelectorAll(".scenario-screen-cb").forEach(function (cb) { cb.checked = false; });
    updateScenarioGenerateEnabled();
  });
  function buildScenarioScreensContext() {
    return scenarioSelectedScreens().map(function (screen) {
      return {
        screen_name: screen.screen_name,
        elements: (screen.elements || []).map(function (el) {
          return { unique_id: el.unique_id, id_source: el.id_source, class: el.class, label: el.label, description: el.description };
        }),
      };
    });
  }
  function renderScenarioList(scenarios) {
    if (!scenarios || !scenarios.length) {
      scenarioResultHost.innerHTML = '<div class="empty-state">沒有產生出情境，可能是選的畫面元件太少或太單純，換選幾個畫面再試試。</div>';
      return;
    }
    var html = "";
    scenarios.forEach(function (sc, i) {
      html += '<div class="script-step-card">' +
        '<div class="script-step-index">' + (i + 1) + '</div>' +
        '<div class="script-step-main">' +
        '<div class="script-step-top"><span class="script-step-action">' + escapeHtml(sc.title || ("情境" + (i + 1))) + '</span></div>' +
        '<div class="script-step-note">' + escapeHtml(sc.description || "") + '</div>' +
        '<div class="script-step-target">涉及畫面：' + escapeHtml((sc.screens_involved || []).join("、") || "（未標明）") + '</div>' +
        '<div class="btn-row" style="margin-top:6px;"><button class="btn primary" data-scenario-idx="' + i + '">➡ 用這個情境產生測試案例</button></div>' +
        '</div></div>';
    });
    scenarioResultHost.innerHTML = html;
    scenarioResultHost.querySelectorAll("[data-scenario-idx]").forEach(function (btn) {
      btn.addEventListener("click", function () { sendScenarioToLlmgen(scenarios[Number(btn.dataset.scenarioIdx)]); });
    });
  }
  scenarioGenerateBtn.addEventListener("click", async function () {
    scenarioGenerateBtn.disabled = true;
    scenarioGenerateBtn.textContent = "分析中...";
    scenarioResultHost.innerHTML = '<div class="empty-state">LLM 分析中，請稍候…</div>';
    try {
      var base = API_BASE;
      var reqBody = { screens_context: buildScenarioScreensContext() };
      var customPrompt = scenarioPromptInput.value.trim();
      if (customPrompt) reqBody.prompt_template = customPrompt;
      var res = await fetch(base + "/llm_scenarios", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(reqBody),
      });
      var data = await res.json();
      if (data.error) {
        scenarioResultHost.innerHTML = '<div class="empty-state">分析失敗：' + escapeHtml(data.error) + '</div>';
        return;
      }
      scenarioLastList = data.scenarios || [];
      renderScenarioList(scenarioLastList);
    } catch (err) {
      scenarioResultHost.innerHTML = '<div class="empty-state">連線失敗：' + escapeHtml(err.message) + '（viewer_server.py 有啟動嗎？）</div>';
    } finally {
      scenarioGenerateBtn.disabled = false;
      scenarioGenerateBtn.textContent = "🔍 分析並產生情境列表";
    }
  });
  // 把選定的情境交給「測案編輯」：開一份新測案（目前的有修改沒存會先問），畫面清單
  // 只預先勾選這個情境有用到的畫面，聊天輸入框帶入情境描述，切到那個分頁讓使用者
  // 確認後自己送出（不自動送出，情境描述可能需要使用者微調後再送）。
  function sendScenarioToLlmgen(scenario) {
    if (!startNewLlmgenCase()) return;
    llmgenData = scenarioData;
    buildLlmgenScreenChecklist();
    var involved = scenario.screens_involved || [];
    llmgenScreenChecklist.querySelectorAll(".llmgen-screen-cb").forEach(function (cb) {
      var idx = Number(cb.value);
      var screenName = llmgenData.screens[idx] && llmgenData.screens[idx].screen_name;
      cb.checked = involved.length === 0 ? true : involved.indexOf(screenName) !== -1;
    });
    updateLlmgenSendEnabled();
    llmgenInput.value = (scenario.title ? (scenario.title + "：") : "") + (scenario.description || "");
    document.querySelector('.tab-btn[data-tab="llmgen"]').click();
    llmgenInput.focus();
  }

  // ================= 測案編輯：新增／開啟／匯入測案，用 LLM 對話產生或修改 =================
  var llmgenNewBtn = document.getElementById("llmgenNewBtn");
  var llmgenCaseStatus = document.getElementById("llmgenCaseStatus");
  var llmgenScreenChecklist = document.getElementById("llmgenScreenChecklist");
  var llmgenScreensCount = document.getElementById("llmgenScreensCount");
  var llmgenSelectAllBtn = document.getElementById("llmgenSelectAllBtn");
  var llmgenSelectNoneBtn = document.getElementById("llmgenSelectNoneBtn");
  var llmgenMessages = document.getElementById("llmgenMessages");
  var llmgenInput = document.getElementById("llmgenInput");
  var llmgenSendBtn = document.getElementById("llmgenSendBtn");
  var llmgenViewPromptBtn = document.getElementById("llmgenViewPromptBtn");
  var llmgenClearBtn = document.getElementById("llmgenClearBtn");
  var llmgenCaseJson = document.getElementById("llmgenCaseJson");
  var llmgenExportBtn = document.getElementById("llmgenExportBtn");
  var llmgenToRunBtn = document.getElementById("llmgenToRunBtn");
  var llmgenSaveToProjectBtn = document.getElementById("llmgenSaveToProjectBtn");
  var llmgenAttachTray = document.getElementById("llmgenAttachTray");
  var llmgenAttachBtn = document.getElementById("llmgenAttachBtn");
  var llmgenAttachInput = document.getElementById("llmgenAttachInput");
  var llmgenAttachHint = document.getElementById("llmgenAttachHint");
  var llmgenPendingAttachments = [];
  var llmgenCaseSyncBadge = document.getElementById("llmgenCaseSyncBadge");
  var llmgenCaseEditor = document.getElementById("llmgenCaseEditor");
  var llmgenVarsCheckBtn = document.getElementById("llmgenVarsCheckBtn");
  var llmgenEditBtn = document.getElementById("llmgenEditBtn");
  var llmgenEditSaveBtn = document.getElementById("llmgenEditSaveBtn");
  var llmgenEditCancelBtn = document.getElementById("llmgenEditCancelBtn");
  var llmgenSyncReviewBtn = document.getElementById("llmgenSyncReviewBtn");
  var llmgenData = null, llmgenChatHistory = [], llmgenLatestCase = null;
  // 目前這份測案是哪來的、有沒有存：llmgenProjectName＝在專案裡的名稱（開啟或存過才有），
  // llmgenFileName＝匯入的檔名，llmgenSavedText＝上次開啟／存檔／匯出時的內容，跟現在不同就是有修改還沒存
  var llmgenProjectName = null, llmgenFileName = null, llmgenSavedText = null;
  var LLMGEN_DEFAULT_PLACEHOLDER = "例如：在自選庫存點第一支股票，確認能進到個股總覽頁";
  // llmgenCaseUnsynced：測案被手動改過、還沒放進對話紀錄，LLM 看到的還是舊版
  var llmgenCaseUnsynced = false, llmgenEditing = false;

  // 給 LLM 對話式產生測案用：LLM 只需要知道「有哪些元件、長怎樣」來寫出
  // 測案步驟，不需要真的拿去定位，欄位刻意精簡省token。
  function shortScreensToLlmgenFormat(srcScreens) {
    return srcScreens.map(function (s) {
      return {
        screen_name: s.name,
        elements: (s.els || []).map(function (el) {
          return { unique_id: el.u, id_source: el.src, class: el.c, label: el.l, description: el.d };
        }),
      };
    });
  }
  // 給「測案執行」用：這份資料是真的要送進 run_tap/run_assert_visible 等
  // 執行邏輯做定位，上面那份精簡格式漏了 native_id／bounds／value／
  // custom_xpath，執行端組不出真正的定位資訊，一律失敗——這裡要用完整欄位。
  function shortScreensToExecutionFormat(srcScreens) {
    return srcScreens.map(function (s) {
      return {
        screen_name: s.name,
        elements: (s.els || []).map(function (el) {
          var out = {
            unique_id: el.u, id_source: el.src, native_id: el.n,
            class: el.c, label: el.l, value: el.v, bounds: el.b, description: el.d
          };
          if (el.fp) out.fingerprint = el.fp;
          if (el.custom_xpath) out.custom_xpath = el.custom_xpath;
          return out;
        }),
      };
    });
  }
  function initLlmgenFromCurrentMap() {
    llmgenData = { screens: shortScreensToLlmgenFormat(screens) };
    buildLlmgenScreenChecklist();
  }
  function buildLlmgenScreenChecklist() {
    llmgenScreenChecklist.innerHTML = "";
    (llmgenData.screens || []).forEach(function (screen, idx) {
      var label = document.createElement("label");
      label.className = "filter-chip";
      label.innerHTML = '<input type="checkbox" class="llmgen-screen-cb" value="' + idx + '" checked /> ' +
        escapeHtml(screen.screen_name) + "（" + (screen.elements || []).length + "）";
      llmgenScreenChecklist.appendChild(label);
    });
    llmgenScreenChecklist.querySelectorAll(".llmgen-screen-cb").forEach(function (cb) { cb.addEventListener("change", updateLlmgenSendEnabled); });
    updateLlmgenSendEnabled();
  }
  function llmgenSelectedScreens() {
    var idxs = Array.prototype.slice.call(llmgenScreenChecklist.querySelectorAll(".llmgen-screen-cb:checked")).map(function (cb) { return Number(cb.value); });
    return idxs.map(function (i) { return llmgenData.screens[i]; });
  }
  function updateLlmgenSendEnabled() {
    var selectedCount = llmgenSelectedScreens().length;
    var totalCount = (llmgenData && llmgenData.screens || []).length;
    llmgenScreensCount.textContent = "已選 " + selectedCount + " / " + totalCount;
    var hasSelection = selectedCount > 0;
    llmgenInput.disabled = !hasSelection;
    llmgenSendBtn.disabled = !hasSelection;
    llmgenAttachBtn.disabled = !hasSelection;
    llmgenViewPromptBtn.disabled = !hasSelection;
    llmgenClearBtn.disabled = llmgenChatHistory.length === 0;
    // 「同步並請 LLM 檢查」要有勾選畫面才能送；初始化時按鈕可能還沒抓到，先略過
    if (llmgenEditBtn) updateLlmgenCaseControls();
  }
  llmgenSelectAllBtn.addEventListener("click", function () {
    llmgenScreenChecklist.querySelectorAll(".llmgen-screen-cb").forEach(function (cb) { cb.checked = true; });
    updateLlmgenSendEnabled();
  });
  llmgenSelectNoneBtn.addEventListener("click", function () {
    llmgenScreenChecklist.querySelectorAll(".llmgen-screen-cb").forEach(function (cb) { cb.checked = false; });
    updateLlmgenSendEnabled();
  });
  function buildScreensContext() {
    return llmgenSelectedScreens().map(function (screen) {
      return {
        screen_name: screen.screen_name,
        elements: (screen.elements || []).map(function (el) {
          return { unique_id: el.unique_id, id_source: el.id_source, class: el.class, label: el.label, description: el.description };
        }),
      };
    });
  }
  function appendChatMessage(role, text) {
    var div = document.createElement("div");
    div.className = "chat-msg " + role;
    div.textContent = text;
    llmgenMessages.appendChild(div);
    llmgenMessages.scrollTop = llmgenMessages.scrollHeight;
    return div;
  }
  function extractJsonCodeBlock(text) {
    var match = text.match(/```json\s*([\s\S]*?)```/i) || text.match(/```\s*([\s\S]*?)```/);
    if (!match) return null;
    try { return JSON.parse(match[1]); } catch (err) { return null; }
  }
  // manual=true 代表這份是使用者手動改的，LLM 還不知道；LLM 產生／匯入的版本本來就在對話紀錄裡
  function renderLlmgenCase(testCase, manual) {
    llmgenLatestCase = testCase;
    llmgenCaseUnsynced = !!manual;
    llmgenCaseJson.textContent = JSON.stringify(testCase, null, 2);
    updateLlmgenCaseControls();
  }
  function updateLlmgenCaseControls() {
    llmgenCaseJson.hidden = llmgenEditing;
    llmgenCaseEditor.hidden = !llmgenEditing;
    llmgenEditBtn.hidden = llmgenEditing;
    llmgenEditSaveBtn.hidden = !llmgenEditing;
    llmgenEditCancelBtn.hidden = !llmgenEditing;
    llmgenEditBtn.disabled = !llmgenLatestCase;
    llmgenCaseSyncBadge.hidden = !llmgenCaseUnsynced;
    llmgenSyncReviewBtn.disabled = !llmgenCaseUnsynced || llmgenEditing || llmgenSelectedScreens().length === 0;
    llmgenExportBtn.disabled = !llmgenLatestCase || llmgenEditing;
    llmgenToRunBtn.disabled = !llmgenLatestCase || llmgenEditing;
    llmgenSaveToProjectBtn.disabled = !llmgenLatestCase || llmgenEditing || !currentProject;
    llmgenVarsCheckBtn.disabled = !llmgenLatestCase || llmgenEditing || !currentProject;
    renderLlmgenCaseStatus();
  }
  function llmgenHasUnsaved() {
    return !!llmgenLatestCase && JSON.stringify(llmgenLatestCase) !== llmgenSavedText;
  }
  // ① 測案：選單停在目前開啟的專案測案；下面那行寫狀態（專案測案的名稱已經在選單上，不重複寫）
  function renderLlmgenCaseStatus() {
    llmgenCaseProjectSelect.value = llmgenProjectName || "";
    if (!llmgenLatestCase) {
      setSourceLine(llmgenCaseStatus, "狀態", "新測案", "還沒產生：在下面選畫面，跟 LLM 描述要測的流程");
      return;
    }
    var unsaved = llmgenHasUnsaved();
    if (llmgenProjectName) {
      setSourceLine(llmgenCaseStatus, "狀態", unsaved ? "⚠️ 有修改還沒存" : "已存進專案", null);
      return;
    }
    var state = currentProject ? "⚠️ 還沒存進專案"
      : unsaved ? "⚠️ 沒開專案，記得按「⬇ 匯出 JSON」存成檔案" : "已匯出";
    setSourceLine(llmgenCaseStatus, "狀態", state,
      (llmgenFileName ? "匯入的檔案 " + llmgenFileName + "：" : "新測案：") + (llmgenLatestCase.test_name || "（未命名）"));
  }
  // 開始一份新的測案：清掉對話和目前的測案。目前這份有修改還沒存會先問，回傳 false＝使用者取消
  function startNewLlmgenCase() {
    if (llmgenEditing) { alert("測試案例還在編輯中，請先儲存或取消"); return false; }
    if (llmgenHasUnsaved() && !confirm("目前的測案「" + (llmgenLatestCase.test_name || "（未命名）") + "」有修改還沒存，換一份會丟掉這些修改，確定嗎？")) return false;
    llmgenChatHistory = [];
    llmgenMessages.innerHTML = '<div class="empty-state">選好畫面後，用自然語言描述你想測試的流程。</div>';
    llmgenLatestCase = null;
    llmgenCaseUnsynced = false;
    llmgenProjectName = null; llmgenFileName = null; llmgenSavedText = null;
    llmgenCaseJson.textContent = "（尚未產生）";
    llmgenPendingAttachments = [];
    renderLlmgenAttachTray();
    llmgenInput.placeholder = LLMGEN_DEFAULT_PLACEHOLDER;
    updateLlmgenCaseControls();
    updateLlmgenSendEnabled();
    updateLlmgenAttachHint();
    return true;
  }
  llmgenNewBtn.addEventListener("click", function () {
    if (startNewLlmgenCase()) llmgenInput.focus();
  });
  // 自動勾選測案裡用到的畫面，回傳目前資料來源找不到的畫面名稱
  function checkLlmgenScreensUsedBy(testCase) {
    var usedScreens = [];
    testCase.steps.forEach(function (s) { if (s.screen_name && usedScreens.indexOf(s.screen_name) < 0) usedScreens.push(s.screen_name); });
    var foundScreens = [];
    llmgenScreenChecklist.querySelectorAll(".llmgen-screen-cb").forEach(function (cb) {
      var name = llmgenData.screens[Number(cb.value)].screen_name;
      if (usedScreens.indexOf(name) >= 0) { cb.checked = true; foundScreens.push(name); }
    });
    updateLlmgenSendEnabled();
    return usedScreens.filter(function (n) { return foundScreens.indexOf(n) < 0; });
  }
  // 手動修改後的版本放進對話紀錄（使用者貼上新版＋助理確認收到），做法跟「匯入既有測試案例」
  // 一樣，只是接在目前對話後面、不清空——LLM 之前的討論還留著，只是之後改以這份為準
  function syncManualCaseToLlmgen() {
    if (!llmgenLatestCase || !llmgenCaseUnsynced) return;
    var caseText = JSON.stringify(llmgenLatestCase, null, 2);
    llmgenChatHistory.push(
      { role: "user", text: "我手動修改了測試案例，以下是修改後的最新完整版本。之後請以這份為準，" +
          "沒提到要改的地方請保持我修改後的樣子，不要改回你之前的版本：\n```json\n" + caseText + "\n```" },
      { role: "model", text: "收到，之後會以你手動修改後的版本（" + llmgenLatestCase.steps.length +
          " 個步驟）為基礎修改。\n```json\n" + caseText + "\n```" }
    );
    var emptyState = llmgenMessages.querySelector(".empty-state");
    if (emptyState) emptyState.remove();
    appendChatMessage("info-note", "🔄 已把手動修改後的測案同步給 LLM（" + llmgenLatestCase.steps.length + " 個步驟），之後的調整會以這份為基礎");
    llmgenCaseUnsynced = false;
    updateLlmgenCaseControls();
    updateLlmgenSendEnabled();
  }
  llmgenEditBtn.addEventListener("click", function () {
    if (!llmgenLatestCase) return;
    llmgenEditing = true;
    llmgenCaseEditor.value = JSON.stringify(llmgenLatestCase, null, 2);
    updateLlmgenCaseControls();
    llmgenCaseEditor.focus();
  });
  llmgenEditCancelBtn.addEventListener("click", function () {
    llmgenEditing = false;
    updateLlmgenCaseControls();
  });
  llmgenEditSaveBtn.addEventListener("click", function () {
    var edited;
    try { edited = JSON.parse(llmgenCaseEditor.value); } catch (err) { alert("JSON 格式錯誤，還沒儲存：" + err.message); return; }
    if (!edited || !Array.isArray(edited.steps)) { alert("找不到 steps 陣列，還沒儲存"); return; }
    if (JSON.stringify(edited) === JSON.stringify(llmgenLatestCase)) {
      llmgenEditing = false;
      updateLlmgenCaseControls();
      return;
    }
    // target 對不上目前資料的步驟先提醒（執行時會找不到元件），但仍讓使用者決定要不要存
    if (llmgenData) {
      var unknown = [];
      edited.steps.forEach(function (step, i) {
        if (!step.target || step.action === "assert_banner") return;
        var screen = (llmgenData.screens || []).find(function (sc) { return sc.screen_name === step.screen_name; });
        var ok = screen && (screen.elements || []).some(function (el) { return el.unique_id === step.target; });
        if (!ok) unknown.push("第 " + (i + 1) + " 步：" + step.target);
      });
      if (unknown.length && !confirm("這些步驟的 target 在目前的畫面資料裡找不到，執行時會失敗：\n\n" +
          unknown.join("\n") + "\n\n仍要儲存嗎？")) return;
    }
    llmgenEditing = false;
    renderLlmgenCase(edited, true);
    if (llmgenData) checkLlmgenScreensUsedBy(edited);
  });
  llmgenSyncReviewBtn.addEventListener("click", function () {
    syncManualCaseToLlmgen();
    llmgenInput.value = "請檢查我剛剛手動調整的部分（跟你前一版比較），確認：target 的 unique_id 都存在於畫面資料、" +
      "screen_name 跟 target 所在的畫面一致、步驟順序合理、assert_banner 緊接在觸發它的 tap 後面。" +
      "有問題就修正，說明改了哪幾步、為什麼，並輸出修改後的完整 JSON；沒問題就說明檢查結果，並原樣輸出完整 JSON。";
    sendLlmgenMessage();
  });
  // ---- 附件（流程圖、規格書 PDF…）----
  // 伺服器不保存對話，附件跟著那則訊息存在 llmgenChatHistory 裡、每輪都整包重送，
  // 所以要控制總大小：Gemini 單次請求 inline 資料上限約 20MB，留一點給文字跟畫面資料
  var LLMGEN_ATTACH_MAX_TOTAL = 18 * 1024 * 1024;
  var LLMGEN_ATTACH_TYPES = {
    "image/png": "image", "image/jpeg": "image", "image/webp": "image", "application/pdf": "PDF",
    "text/plain": "TXT", "text/markdown": "MD", "text/csv": "CSV", "application/json": "JSON",
  };
  var LLMGEN_ATTACH_EXT_TYPES = {
    png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", webp: "image/webp", pdf: "application/pdf",
    txt: "text/plain", md: "text/markdown", markdown: "text/markdown", csv: "text/csv", json: "application/json",
  };
  function attachmentMime(file) {
    if (LLMGEN_ATTACH_TYPES[file.type]) return file.type;
    // 瀏覽器對 .md 等副檔名常常給空的 type，退回看副檔名
    var ext = (file.name.split(".").pop() || "").toLowerCase();
    return LLMGEN_ATTACH_EXT_TYPES[ext] || null;
  }
  function formatBytes(n) {
    return n >= 1024 * 1024 ? (n / 1024 / 1024).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB";
  }
  function historyAttachments() {
    return llmgenChatHistory.reduce(function (all, m) { return all.concat(m.attachments || []); }, []);
  }
  function totalAttachmentBytes(list) {
    return list.reduce(function (sum, a) { return sum + (a.size || 0); }, 0);
  }
  function readFileAsAttachment(file, mime) {
    return new Promise(function (resolve, reject) {
      var reader = new FileReader();
      reader.onload = function () {
        resolve({ name: file.name || "貼上的圖片.png", mime: mime, size: file.size, data: String(reader.result).split(",")[1] });
      };
      reader.onerror = function () { reject(reader.error); };
      reader.readAsDataURL(file);
    });
  }
  async function addLlmgenAttachments(fileList) {
    var files = Array.prototype.slice.call(fileList || []);
    if (!files.length) return;
    var rejected = [], tooBig = [];
    var used = totalAttachmentBytes(historyAttachments()) + totalAttachmentBytes(llmgenPendingAttachments);
    for (var i = 0; i < files.length; i++) {
      var file = files[i], mime = attachmentMime(file);
      if (!mime) { rejected.push(file.name); continue; }
      if (llmgenPendingAttachments.some(function (a) { return a.name === file.name && a.size === file.size; })) continue;
      if (used + file.size > LLMGEN_ATTACH_MAX_TOTAL) { tooBig.push(file.name + "（" + formatBytes(file.size) + "）"); continue; }
      try {
        llmgenPendingAttachments.push(await readFileAsAttachment(file, mime));
        used += file.size;
      } catch (err) { rejected.push(file.name); }
    }
    renderLlmgenAttachTray();
    var notes = [];
    if (rejected.length) notes.push("不支援的格式（Word／Excel 請先另存成 PDF）：" + rejected.join("、"));
    if (tooBig.length) notes.push("超過附件總大小上限 " + formatBytes(LLMGEN_ATTACH_MAX_TOTAL) + "，沒有加入：" + tooBig.join("、"));
    if (notes.length) alert(notes.join("\n\n"));
  }
  function attachmentItemHtml(att, removeIndex) {
    var kind = LLMGEN_ATTACH_TYPES[att.mime] || "FILE";
    var remove = removeIndex === undefined ? "" :
      '<button class="attach-remove" data-index="' + removeIndex + '" title="移除" aria-label="移除 ' + escapeHtml(att.name) + '">✕</button>';
    if (kind === "image") {
      return '<div class="attach-item image" title="' + escapeHtml(att.name + "・" + formatBytes(att.size)) + '">' +
        '<img src="data:' + att.mime + ";base64," + att.data + '" alt="' + escapeHtml(att.name) + '">' + remove + "</div>";
    }
    return '<div class="attach-item doc" title="' + escapeHtml(att.name) + '">' +
      '<div class="attach-doc-icon' + (kind === "PDF" ? " pdf" : "") + '">' + kind + "</div>" +
      '<div class="attach-doc-meta"><span class="attach-doc-name">' + escapeHtml(att.name) + "</span>" +
      '<span class="attach-doc-sub">' + kind + "・" + formatBytes(att.size) + "</span></div>" + remove + "</div>";
  }
  function renderLlmgenAttachTray() {
    llmgenAttachTray.hidden = llmgenPendingAttachments.length === 0;
    llmgenAttachTray.innerHTML = llmgenPendingAttachments.map(function (att, i) { return attachmentItemHtml(att, i); }).join("");
    updateLlmgenAttachHint();
  }
  function updateLlmgenAttachHint() {
    var sent = historyAttachments();
    var total = totalAttachmentBytes(sent) + totalAttachmentBytes(llmgenPendingAttachments);
    if (!sent.length && !llmgenPendingAttachments.length) { llmgenAttachHint.textContent = ""; return; }
    var parts = [];
    if (sent.length) parts.push("對話中已有 " + sent.length + " 個附件（之後每輪都會一起送給 LLM，計入 token 用量）");
    if (llmgenPendingAttachments.length) parts.push("待送出 " + llmgenPendingAttachments.length + " 個");
    llmgenAttachHint.textContent = "📎 " + parts.join("，") + "・合計 " + formatBytes(total) + " / " + formatBytes(LLMGEN_ATTACH_MAX_TOTAL);
    llmgenAttachHint.className = "attach-hint" + (total > LLMGEN_ATTACH_MAX_TOTAL * 0.8 ? " warn" : "");
  }
  llmgenAttachTray.addEventListener("click", function (e) {
    var btn = e.target.closest(".attach-remove");
    if (!btn) return;
    llmgenPendingAttachments.splice(Number(btn.dataset.index), 1);
    renderLlmgenAttachTray();
  });
  llmgenAttachBtn.addEventListener("click", function () { llmgenAttachInput.click(); });
  llmgenAttachInput.addEventListener("change", function (e) {
    var files = e.target.files;
    addLlmgenAttachments(files).then(function () { e.target.value = ""; });
  });
  llmgenInput.addEventListener("paste", function (e) {
    var files = Array.prototype.slice.call((e.clipboardData && e.clipboardData.files) || []);
    if (!files.length) return;  // 純文字照常貼上
    e.preventDefault();
    addLlmgenAttachments(files);
  });
  var llmgenChatBox = llmgenMessages.closest(".chat-box");
  llmgenChatBox.addEventListener("dragover", function (e) {
    if (llmgenAttachBtn.disabled || !e.dataTransfer || Array.prototype.indexOf.call(e.dataTransfer.types, "Files") < 0) return;
    e.preventDefault();
    llmgenChatBox.classList.add("drag-over");
  });
  llmgenChatBox.addEventListener("dragleave", function (e) {
    if (!llmgenChatBox.contains(e.relatedTarget)) llmgenChatBox.classList.remove("drag-over");
  });
  llmgenChatBox.addEventListener("drop", function (e) {
    llmgenChatBox.classList.remove("drag-over");
    if (llmgenAttachBtn.disabled || !e.dataTransfer || !e.dataTransfer.files.length) return;
    e.preventDefault();
    addLlmgenAttachments(e.dataTransfer.files);
  });
  function appendUserMessage(text, attachments) {
    var div = appendChatMessage("user", "");
    if (attachments && attachments.length) {
      var tray = document.createElement("div");
      tray.className = "attach-tray";
      tray.innerHTML = attachments.map(function (att) { return attachmentItemHtml(att); }).join("");
      div.appendChild(tray);
    }
    div.appendChild(document.createTextNode(text));
    llmgenMessages.scrollTop = llmgenMessages.scrollHeight;
    return div;
  }

  async function sendLlmgenMessage() {
    var text = llmgenInput.value.trim();
    var attachments = llmgenPendingAttachments.slice();
    if (!text && !attachments.length) return;
    if (!text) text = "請依照附件內容產生測試案例";
    if (llmgenEditing) { alert("測試案例還在編輯中，請先儲存或取消，再跟 LLM 對話"); return; }
    // 手動改過但還沒同步就直接講下一個指令：先自動同步，不然 LLM 會拿舊版來改，把手動的修改蓋掉
    if (llmgenCaseUnsynced) syncManualCaseToLlmgen();
    var emptyState = llmgenMessages.querySelector(".empty-state");
    if (emptyState) emptyState.remove();
    appendUserMessage(text, attachments);
    var userMessage = { role: "user", text: text };
    if (attachments.length) userMessage.attachments = attachments;
    llmgenChatHistory.push(userMessage);
    var typedText = llmgenInput.value;
    llmgenInput.value = "";
    llmgenPendingAttachments = [];
    renderLlmgenAttachTray();
    llmgenInput.disabled = true;
    llmgenSendBtn.disabled = true;
    llmgenAttachBtn.disabled = true;
    // 失敗時把這則訊息的文字、附件放回輸入區，使用者不用重選檔案
    function restoreUnsentMessage() {
      llmgenChatHistory.pop();
      llmgenInput.value = typedText;
      llmgenPendingAttachments = attachments.concat(llmgenPendingAttachments);
      renderLlmgenAttachTray();
    }
    llmgenSendBtn.textContent = "傳送中...";
    var thinkingMsg = appendChatMessage("model", "思考中...");
    try {
      var serverUrl = API_BASE;
      var res = await fetch(serverUrl + "/llm_chat", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: llmgenChatHistory, screens_context: buildScreensContext(),
          project: currentProject ? currentProject.id : null }),
      });
      var data = await res.json();
      thinkingMsg.remove();
      if (data.error) {
        appendChatMessage("system-note", "LLM 呼叫失敗：" + data.error + "（訊息和附件已放回輸入區）");
        restoreUnsentMessage();
      } else {
        appendChatMessage("model", data.reply);
        llmgenChatHistory.push({ role: "model", text: data.reply });
        var parsed = extractJsonCodeBlock(data.reply);
        if (parsed && Array.isArray(parsed.steps)) {
          renderLlmgenCase(parsed);
          openVarsNeed(parsed, true);  // 用到還沒有值的變數才跳出來
        }
      }
    } catch (err) {
      thinkingMsg.remove();
      appendChatMessage("system-note", failText("連線失敗", err, "（請確認 viewer_server.py 已啟動；訊息和附件已放回輸入區）"));
      restoreUnsentMessage();
    } finally {
      llmgenInput.disabled = false;
      llmgenSendBtn.disabled = false;
      llmgenAttachBtn.disabled = false;
      llmgenSendBtn.textContent = "送出";
      updateLlmgenAttachHint();
      llmgenClearBtn.disabled = llmgenChatHistory.length === 0;
      llmgenInput.focus();
    }
  }
  llmgenSendBtn.addEventListener("click", sendLlmgenMessage);
  llmgenInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); sendLlmgenMessage(); }
  });
  llmgenViewPromptBtn.addEventListener("click", function () {
    alert(
      "系統提示詞由 viewer_server.py 依你勾選的畫面資料組出，重點規則：\n\n" +
      "1. 輸出必須是 ```json 包起來的測試案例\n2. action 只能是 tap／input_text／assert_visible／assert_text_contains／assert_banner（驗證短暫出現的跑馬燈；連續幾個會合併監看，加 \"in_order\": true 要求比前一個晚出現）\n" +
      "3. target 必須是勾選畫面裡真實存在的 unique_id，不可以憑空編造\n4. 測試案例可以橫跨多個畫面\n" +
      "5. assert_visible／assert_text_contains 可加 timeout（秒），畫面會晚幾秒才更新時，在期限內反覆檢查直到通過\n" +
      "6. 附件（流程圖、PDF…）只當作「要測什麼」的參考，target 仍然只能用勾選畫面裡的 unique_id；對應不到元件的步驟會列出來，建議補掃畫面"
    );
  });
  llmgenClearBtn.addEventListener("click", function () {
    if (!confirm("確定要清空目前的對話紀錄嗎？（已產生的測試案例不會被清除）")) return;
    llmgenChatHistory = [];
    llmgenMessages.innerHTML = '<div class="empty-state">對話已清空，可以重新開始描述你想要的測試流程。</div>';
    updateLlmgenSendEnabled();
    updateLlmgenAttachHint();
  });
  // 匯入之前產生（或手調過）的測試案例當作修改基礎：開一段新對話，把測案塞進
  // 對話歷史的第一輪（使用者貼上測案 + 助理確認收到），之後使用者講要怎麼改，
  // LLM 就會像修改自己上一輪的輸出一樣，重新輸出完整 JSON——不需要動伺服器端。
  var llmgenCaseImportInput = document.getElementById("llmgenCaseImportInput");
  llmgenCaseImportInput.addEventListener("change", function (e) {
    var file = e.target.files[0]; if (!file) return;
    e.target.value = "";
    var reader = new FileReader();
    reader.onload = function () {
      var testCase;
      try { testCase = JSON.parse(reader.result); } catch (err) { alert("JSON 格式錯誤：" + err.message); return; }
      importLlmgenCase(testCase, "📂 匯入測案：" + file.name, { fileName: file.name });
    };
    reader.readAsText(file);
  });
  // sourceLabel：對話裡顯示「從哪裡來」；origin：{ projectName } 或 { fileName }，顯示在 ① 測案下面
  function importLlmgenCase(testCase, sourceLabel, origin) {
    if (!testCase || !Array.isArray(testCase.steps)) { alert("這不是測試案例 JSON（找不到 steps 陣列）"); return; }
    if (!startNewLlmgenCase()) return;
    llmgenProjectName = origin.projectName || null;
    llmgenFileName = origin.fileName || null;
    llmgenSavedText = JSON.stringify(testCase);
    // 自動勾選測案裡用到的畫面，找不到的畫面列出來提醒使用者
    var missingScreens = checkLlmgenScreensUsedBy(testCase);

    var caseText = JSON.stringify(testCase, null, 2);
    var ackText = "已讀入既有的測試案例「" + (testCase.test_name || "（未命名）") + "」（" + testCase.steps.length +
      " 個步驟），請告訴我要怎麼調整，我會輸出修改後的完整 JSON。";
    llmgenChatHistory = [
      { role: "user", text: "以下是一份之前產生的測試案例，請以它為基礎，依照我接下來的指示修改（沒提到要改的步驟請保持原樣）：\n```json\n" + caseText + "\n```" },
      { role: "model", text: ackText + "\n```json\n" + caseText + "\n```" },
    ];
    llmgenMessages.innerHTML = "";
    appendChatMessage("user", sourceLabel);
    appendChatMessage("model", ackText);
    if (missingScreens.length) {
      appendChatMessage("system-note", "目前資料來源找不到這些畫面：" + missingScreens.join("、") +
        "。LLM 看不到它們的元件，改到這些步驟時可能無法確認 unique_id，建議換成對應的地圖再匯入。");
    }
    renderLlmgenCase(testCase);
    updateLlmgenSendEnabled();
    updateLlmgenAttachHint();
    llmgenInput.placeholder = "例如：第 3 步之後加一個驗證「委託成功」文字的步驟";
    llmgenInput.focus();
  }
  llmgenExportBtn.addEventListener("click", function () {
    if (!llmgenLatestCase) return;
    if (!llmgenProjectName) { llmgenSavedText = JSON.stringify(llmgenLatestCase); renderLlmgenCaseStatus(); }
    var blob = new Blob([JSON.stringify(llmgenLatestCase, null, 2)], { type: "application/json" });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    var safeName = (llmgenLatestCase.test_name || "llm_test_case").replace(/[^\w一-鿿-]+/g, "_");
    a.href = url; a.download = safeName + ".json"; a.click();
    URL.revokeObjectURL(url);
  });
  llmgenToRunBtn.addEventListener("click", function () {
    if (!llmgenLatestCase) return;
    runscriptTestCase = llmgenLatestCase;
    var unsaved = llmgenHasUnsaved();
    runscriptCaseRef = llmgenProjectName && !unsaved ? llmgenProjectName : null;
    runscriptEditorLabel = (llmgenProjectName || llmgenLatestCase.test_name || "（未命名）") +
      (llmgenProjectName ? "（有修改還沒存）" : "（還沒存進專案）");
    renderRunscriptPreview();
    setRunMode("single");
    document.querySelector('.tab-btn[data-tab="runscript"]').click();
  });

  // ================= 測案執行（整合自 viewer.html）=================
  var runscriptServerStatus = document.getElementById("runscriptServerStatus");
  var runscriptServerCheckBtn = document.getElementById("runscriptServerCheckBtn");
  var runscriptRunBtn = document.getElementById("runscriptRunBtn");
  var runscriptStopBtn = document.getElementById("runscriptStopBtn");
  var runscriptCheckScreen = document.getElementById("runscriptCheckScreen");
  var runscriptDismissDialogs = document.getElementById("runscriptDismissDialogs");
  var runscriptVarsPick = document.getElementById("runscriptVarsPick");
  var runscriptVars = [];  // 單份執行選的變數檔名稱，後面的蓋前面的
  var runscriptBody = document.getElementById("runscriptBody");
  // 地圖一律用「地圖檢視」目前開著的那張（執行當下才抓，地圖換了會跟著變）
  // runscriptCaseRef：選的是專案測案時的名稱；null＝測案編輯送來、不在專案裡（或有修改沒存）的那份
  var runscriptTestCase = null, runscriptCaseRef = null, runscriptEditorLabel = "";
  function renderRunscriptSources() {
    renderRunscriptCasePicker();
  }
  function renderRunscriptCasePicker() {
    var fromEditor = runscriptTestCase && !runscriptCaseRef;
    var editorOption = fromEditor ? '<option value="__editor__">➡ 測案編輯送來的：' + escapeHtml(runscriptEditorLabel) + "</option>" : "";
    runscriptCaseProjectSelect.hidden = !currentProject && !fromEditor;
    runscriptCaseProjectSelect.innerHTML = caseOptionsHtml(currentProject ? "📁 選專案裡的測案…" : "（沒開專案）", editorOption);
    runscriptCaseProjectSelect.value = runscriptCaseRef || (fromEditor ? "__editor__" : "");
  }
  function currentExecutionMap() { return { screens: shortScreensToExecutionFormat(screens) }; }

  function findElementInMap(elementMap, screenName, targetId) {
    if (!elementMap) return null;
    var screen = (elementMap.screens || []).find(function (s) { return s.screen_name === screenName; });
    if (!screen) return null;
    return (screen.elements || []).find(function (el) { return el.unique_id === targetId; }) || null;
  }
  function renderRunscriptPreview(results, stopped, precheck, runningIndex) {
    renderRunscriptSources();
    if (!runscriptTestCase) {
      runscriptBody.innerHTML = '<div class="empty-state">選好測案後，這裡會列出所有步驟預覽。</div>';
      runscriptRunBtn.disabled = true;
      return;
    }
    var elementMap = currentExecutionMap();
    var steps = runscriptTestCase.steps || [];
    var html = "";
    if (runscriptTestCase.test_name) {
      html += '<div class="script-note">測試案例：' + escapeHtml(runscriptTestCase.test_name) + "（共 " + steps.length + " 步驟）</div>";
    }
    if (stopped) {
      html += '<div class="script-note">⏹ 測試已被手動停止，之後的步驟沒有執行。</div>';
    }
    if (precheck && precheck.status === "fail") {
      html += '<div class="script-note">⚠️ 手機目前不在第一步的畫面「' + escapeHtml(precheck.screen_name || "") + '」，整份測案沒有執行。請先把手機導回這個畫面再執行；確定畫面正確只是判斷錯誤的話，可以取消勾選「執行前先檢查」。</div>';
    }
    if (results) {
      var passCount = results.filter(function (r) { return r.outcome.indexOf("PASS") === 0; }).length;
      var failCount = results.filter(function (r) { return r.outcome.indexOf("FAIL") === 0; }).length;
      var skipCount = results.filter(function (r) { return r.outcome.indexOf("SKIP") === 0; }).length;
      html += '<div class="compare-summary">' +
        '<div class="summary-card confirmed"><div class="num">' + passCount + '</div><div class="label">PASS</div></div>' +
        '<div class="summary-card missing"><div class="num">' + failCount + '</div><div class="label">FAIL</div></div>' +
        '<div class="summary-card needs_review"><div class="num">' + skipCount + '</div><div class="label">SKIP</div></div>' +
        '</div>';
    }
    if (precheck) {
      var precheckClass = precheck.status === "pass" ? "pass" : precheck.status === "fail" ? "fail" : "skip";
      var precheckLabel = { pass: "PASS", fail: "FAIL", unchecked: "無法校驗", skipped: "已略過" }[precheck.status] || precheck.status;
      html += '<div class="script-step-card ' + precheckClass + '">' +
        '<div class="script-step-index">0</div>' +
        '<div class="script-step-main">' +
        '<div class="script-step-top"><span class="script-step-action">檢查目前畫面</span>' +
        (precheck.screen_name ? '<span class="script-step-screen">' + escapeHtml(precheck.screen_name) + '</span>' : "") + '</div>' +
        '<div class="script-step-outcome ' + precheckClass + '">' + escapeHtml(precheckLabel + "（" + precheck.message + "）") + '</div>' +
        '</div></div>';
    }
    steps.forEach(function (step, i) {
      var result = results ? results[i] : null;
      var outcome = result ? result.outcome : null;
      var isRunning = !outcome && runningIndex === i + 1;
      if (isRunning) outcome = "⏳ 執行中...";
      var outcomeClass = isRunning ? "running" : outcome ? (outcome.indexOf("PASS") === 0 ? "pass" : outcome.indexOf("FAIL") === 0 ? "fail" : "skip") : "";
      var el = findElementInMap(elementMap, step.screen_name, step.target);
      var resolvedLabel = step.action === "assert_banner"
        ? "等待跑馬燈出現（不需要 element_map 元件）" + (step.in_order ? "，須在前一個跑馬燈之後出現" : "")
        : el ? (el.description || el.label || "（無描述）") : "⚠️ 在目前資料裡找不到這個元件";
      var hasValue = step.value !== undefined && step.value !== null;
      html += '<div class="script-step-card ' + outcomeClass + '">' +
        '<div class="script-step-index">' + (i + 1) + '</div>' +
        '<div class="script-step-main">' +
        '<div class="script-step-top"><span class="script-step-action">' + escapeHtml(step.action || "") + '</span>' +
        '<span class="script-step-screen">' + escapeHtml(step.screen_name || "") + '</span></div>' +
        (step.note ? '<div class="script-step-note">' + escapeHtml(step.note) + '</div>' : "") +
        '<div class="script-step-target">' + escapeHtml(step.target || "") + (hasValue ? ' → 「' + escapeHtml(String(step.value)) + '」' : "") + '</div>' +
        '<div class="script-step-target">' + escapeHtml(resolvedLabel) + '</div>' +
        (outcome ? '<div class="script-step-outcome ' + outcomeClass + '">' + escapeHtml(outcome) + '</div>' : "") +
        '</div></div>';
    });
    runscriptBody.innerHTML = html;
    runscriptRunBtn.disabled = false;
  }
  async function checkRunscriptServer() {
    await renderServerStatus(API_BASE, runscriptServerStatus, mapPlatform());
  }
  runscriptServerCheckBtn.addEventListener("click", checkRunscriptServer);
  // 執行中使用者自己捲動畫面（不是程式捲的）：這次執行就不再自動捲到執行中的步驟
  var followRunningStep = false;
  var SCROLL_KEYS = ["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "];
  function stopFollowingRunningStep() { followRunningStep = false; }
  window.addEventListener("wheel", stopFollowingRunningStep, { passive: true });
  window.addEventListener("touchmove", stopFollowingRunningStep, { passive: true });
  window.addEventListener("keydown", function (e) {
    if (SCROLL_KEYS.indexOf(e.key) >= 0 && !/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) stopFollowingRunningStep();
  });
  runscriptRunBtn.addEventListener("click", async function () {
    var elementMap = currentExecutionMap();
    if (!runscriptTestCase) return;
    var stepCount = (runscriptTestCase.steps || []).length;
    var uncovered = currentProject ? uncoveredKeys(runscriptTestCase, runscriptVars) : [];
    if (!confirm('即將依序執行「' + (runscriptTestCase.test_name || "這份測試案例") + '」共 ' + stepCount + ' 個步驟，會實際操作模擬器/裝置' +
      (uncovered.length ? "。\n\n⚠️ 選的變數檔裡沒有：" + uncovered.join("、") + "（有設同名環境變數的話可以忽略），用到的步驟會失敗" : "") +
      "\n\n確定要執行嗎？")) return;
    runscriptRunBtn.disabled = true;
    runscriptRunBtn.textContent = "執行中...";
    runscriptStopBtn.hidden = false;
    runscriptStopBtn.disabled = false;
    runscriptStopBtn.textContent = "⏹ 停止";
    var progressBase = API_BASE;
    var progressTimer = null;
    try {
      await assertPlatformMatches(progressBase, mapPlatform());
      // 執行是同步請求要跑完才回傳，執行中另外輪詢 /test_progress 顯示目前跑到哪一步
      var polling = false, runFinished = false;
      // 換到下一步時才把畫面捲到執行中的那一步；使用者自己捲過（滾輪／觸控／按鍵）就不再跟，
      // 不然卡在同一步時每秒被拉回去，往上捲不到停止按鈕
      followRunningStep = true;
      var lastFollowedStep = null;
      progressTimer = setInterval(async function () {
        if (polling) return;
        polling = true;
        try {
          var pr = await (await fetch(progressBase + "/test_progress")).json();
          if (pr.running && !runFinished) {
            runscriptRunBtn.textContent = "執行中（" + Math.max(pr.current || 0, 0) + "/" + pr.total + "）...";
            renderRunscriptPreview(pr.results, false, pr.precheck, pr.current || 0);
            var running = runscriptBody.querySelector(".script-step-card.running");
            if (running && followRunningStep && pr.current !== lastFollowedStep) {
              lastFollowedStep = pr.current;
              running.scrollIntoView({ block: "nearest" });
            }
            runscriptRunBtn.disabled = true;
          }
        } catch (e) { /* 輪詢失敗不影響主流程 */ }
        polling = false;
      }, 1000);
      var res = await fetch(API_BASE + "/run_test_script", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ element_map: elementMap, test_case: runscriptTestCase, check_screen: runscriptCheckScreen.checked, dismiss_dialogs: runscriptDismissDialogs.checked,
          project: currentProject ? currentProject.id : null, vars: currentProject ? runscriptVars : [] }),
      });
      runFinished = true;
      var data = await res.json();
      if (data.error) alert("執行失敗：" + data.error);
      else renderRunscriptPreview(data.results, data.stopped, data.precheck);
    } catch (err) {
      alert(failText("連線失敗", err, "（請確認 viewer_server.py 已啟動）"));
    } finally {
      if (progressTimer) clearInterval(progressTimer);
      runscriptRunBtn.disabled = false;
      runscriptRunBtn.textContent = "▶ 執行測試";
      runscriptStopBtn.hidden = true;
    }
  });
  runscriptStopBtn.addEventListener("click", async function () {
    runscriptStopBtn.disabled = true;
    runscriptStopBtn.textContent = "停止中（等目前步驟跑完）...";
    try {
      var res = await fetch(API_BASE + "/stop_test_script", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
      });
      var data = await res.json();
      if (data.error || !data.stopped) {
        alert("停止失敗：" + (data.error || data.message));
        runscriptStopBtn.disabled = false;
        runscriptStopBtn.textContent = "⏹ 停止";
      }
    } catch (err) {
      alert("連線失敗：" + err.message);
      runscriptStopBtn.disabled = false;
      runscriptStopBtn.textContent = "⏹ 停止";
    }
  });

  // ================= 批次執行 =================
  // 單份執行／批次執行二選一顯示，裝置橋接兩邊共用；上次用哪個記在這個瀏覽器（只是個人偏好，讀寫失敗就用單份）
  var runModeSingleBtn = document.getElementById("runModeSingleBtn");
  var runModeBatchBtn = document.getElementById("runModeBatchBtn");
  function setRunMode(mode) {
    var batch = mode === "batch";
    document.getElementById("runModeSingle").hidden = batch;
    document.getElementById("runModeBatch").hidden = !batch;
    runModeSingleBtn.setAttribute("aria-selected", String(!batch));
    runModeBatchBtn.setAttribute("aria-selected", String(batch));
    try { localStorage.setItem("runMode", batch ? "batch" : "single"); } catch (e) { /* 無痕模式等情況存不了，不影響使用 */ }
  }
  runModeSingleBtn.addEventListener("click", function () { setRunMode("single"); });
  runModeBatchBtn.addEventListener("click", function () { setRunMode("batch"); });
  try { if (localStorage.getItem("runMode") === "batch") setRunMode("batch"); } catch (e) { /* 照預設用單份 */ }

  // batchGroups：[{ mapName, map（全名格式）, mapRef, setup: {name, case, ref?} | null, cases: [{name, case, ref?, login?, vars?}] }]
  // login＝登入用的變數檔名稱，沒有就是不需登入（照目前狀態直接跑）；vars＝另外要代入的變數檔名稱陣列
  // setupMapRef／setupMap：登入測案用的專案地圖（登入頁另外爬成一張時），null＝跟這組用同一張
  // 批次要開專案才能用：地圖、測案都從專案選，存到專案時 mapRef（專案地圖名稱）、
  // ref（專案測案名稱）只存引用，開啟和執行當下才抓最新版，地圖或測案改了不用重新設定批次。
  // 舊的批次設定裡整份內嵌的地圖／測案照樣能開。
  var batchGroups = [];
  var batchGroupsEl = document.getElementById("batchGroups");
  var batchRunBtn = document.getElementById("batchRunBtn");
  var batchStopBtn = document.getElementById("batchStopBtn");
  var batchAddGroupBtn = document.getElementById("batchAddGroupBtn");
  var batchSaveToProjectBtn = document.getElementById("batchSaveToProjectBtn");
  var batchResultsEl = document.getElementById("batchResults");
  var batchAutoNavigate = document.getElementById("batchAutoNavigate");
  var batchAllowRestart = document.getElementById("batchAllowRestart");
  var batchDismissDialogs = document.getElementById("batchDismissDialogs");
  var batchRunning = false;
  // 使用者手動展開／收合過的結果列（key = 組-份）；沒點過的照狀態決定：執行中、FAIL、SKIP 自動展開
  var batchUserOpen = {};

  function currentMapAsFull() {
    return { app: app, screens: screens.map(shortScreenToFull), edges: edges, skipped: skipped };
  }
  function mapScreenNames(map) {
    return (map && map.screens || []).map(function (s) { return s.screen_name || s.name; });
  }
  function caseStartScreen(testCase) {
    var first = (testCase.steps || []).find(function (s) { return s.action !== "assert_banner"; });
    return first ? first.screen_name : null;
  }
  // 測案用到、但地圖裡沒有的畫面（執行時這些步驟會找不到元件）
  function caseMissingScreens(testCase, map) {
    var names = mapScreenNames(map), missing = [];
    (testCase.steps || []).forEach(function (s) {
      if (s.action !== "assert_banner" && s.screen_name && names.indexOf(s.screen_name) < 0 && missing.indexOf(s.screen_name) < 0) missing.push(s.screen_name);
    });
    return missing;
  }
  function caseRowHtml(gi, ci, item, map, isSetup) {
    var start = caseStartScreen(item.case);
    var missing = map ? caseMissingScreens(item.case, map) : [];
    var n = (item.case.steps || []).length;
    return '<div class="batch-case">' +
      '<span class="idx">' + (isSetup ? "🔑" : ci + 1) + "</span>" +
      '<span class="name" title="' + escapeHtml(item.ref ? "專案測案：" + item.ref : item.name) + '">' + (item.ref ? "📁 " : "") + escapeHtml(item.case.test_name || item.name) + "</span>" +
      '<span class="meta">' + n + " 步・起點「" + escapeHtml(start || "—") + "」</span>" +
      (missing.length ? '<span class="warn" title="這些畫面不在這組的地圖裡，執行時會找不到元件">⚠️ 地圖缺少：' + escapeHtml(missing.join("、")) + "</span>" : "") +
      (isSetup ? "" : loginPickHtml(gi, ci, item.login) +
        varsPickHtml(item.vars, 'data-g="' + gi + '" data-c="' + ci + '"', "case-var")) +
      (isSetup ? "" :
        '<button class="icon-btn" data-act="up" data-g="' + gi + '" data-c="' + ci + '" title="往上移"' + (ci === 0 ? " disabled" : "") + ">↑</button>" +
        '<button class="icon-btn" data-act="down" data-g="' + gi + '" data-c="' + ci + '" title="往下移"' + (ci === batchGroups[gi].cases.length - 1 ? " disabled" : "") + ">↓</button>") +
      '<button class="icon-btn" data-act="' + (isSetup ? "remove-setup" : "remove-case") + '" data-g="' + gi + '" data-c="' + ci + '" title="移除">✕</button>' +
      "</div>";
  }
  function loginPickHtml(gi, ci, login) {
    var names = varFileNames();
    var opts = '<option value="">登入：不需登入</option>' + names.map(function (n) {
      return '<option value="' + escapeHtml(n) + '"' + (n === login ? " selected" : "") + ">🔑 登入：" + escapeHtml(n) + "</option>";
    }).join("");
    if (login && names.indexOf(login) < 0) opts += '<option value="' + escapeHtml(login) + '" selected>⚠️ 登入：' + escapeHtml(login) + "（變數檔裡沒有）</option>";
    return '<select class="project-pick" data-act="case-login" data-g="' + gi + '" data-c="' + ci + '" title="執行這份前先用這個變數檔登入（跟目前登入的不同才會重開 App 重新登入）">' + opts + "</select>";
  }
  function setupMapOf(g) { return g.setupMapRef ? g.setupMap : g.map; }
  function setupMapPickHtml(gi, ref) {
    if (!currentProject) return "";
    var names = projectMaps.map(function (m) { return m.name; });
    return '<select class="project-pick" data-act="setup-map" data-g="' + gi + '" title="登入測案的畫面在哪一張地圖（登入頁另外爬成一張時選它）">' +
      '<option value="">登入地圖：同上面的地圖</option>' +
      names.map(function (n) { return '<option value="' + escapeHtml(n) + '"' + (n === ref ? " selected" : "") + ">登入地圖：" + escapeHtml(n) + "</option>"; }).join("") +
      (ref && names.indexOf(ref) < 0 ? '<option value="' + escapeHtml(ref) + '" selected>⚠️ 登入地圖：' + escapeHtml(ref) + "（專案裡沒有）</option>" : "") +
      "</select>";
  }
  function renderBatchGroups() {
    batchAddGroupBtn.disabled = !currentProject || batchRunning;
    if (!currentProject) {
      batchGroupsEl.innerHTML = '<div class="empty-state">批次執行要先開專案（頁首「專案」選一個）：每一組的地圖、測案都從專案裡選。</div>';
    } else if (!batchGroups.length) {
      batchGroupsEl.innerHTML = '<div class="empty-state">還沒有任何一組。按「＋ 新增一組地圖」開始，或開啟專案裡存的批次。</div>';
    } else {
      batchGroupsEl.innerHTML = batchGroups.map(function (g, gi) {
        if (g.mapRef && g.mapRef === currentMapName) g.map = currentMapAsFull();
        var mapInfo = g.mapRef ? "📁 專案地圖「" + escapeHtml(g.mapRef) + "」（執行時用最新版，" + mapScreenNames(g.map).length + " 個畫面）"
          : g.map ? escapeHtml(g.mapName) + "（" + mapScreenNames(g.map).length + " 個畫面）" : '<span class="warn">尚未選擇地圖</span>';
        return '<div class="batch-group">' +
          '<div class="batch-group-head"><h3>第 ' + (gi + 1) + " 組</h3>" +
          '<button class="icon-btn" data-act="remove-group" data-g="' + gi + '" title="移除這一組">✕ 移除這組</button></div>' +
          '<div class="batch-row"><span class="lbl">地圖</span><span>' + mapInfo + "</span>" +
          '<button class="icon-btn" data-act="use-current-map" data-g="' + gi + '">使用目前地圖</button>' +
          projectMapPickHtml(gi) + "</div>" +
          '<div class="batch-row"><span class="lbl">登入測案</span>' +
          (g.setup ? "" : '<span style="color:var(--ink-soft);">（選填）重開 App 後、或停在登入頁時會先跑它</span>') +
          projectCasePickHtml("project-setup", gi, "📁 從專案選") + setupMapPickHtml(gi, g.setupMapRef) + "</div>" +
          (g.setup ? '<div class="batch-cases">' + caseRowHtml(gi, 0, g.setup, setupMapOf(g), true) + "</div>" : "") +
          '<div class="batch-row"><span class="lbl">測案</span><span style="color:var(--ink-soft);">依序執行</span></div>' +
          '<div class="batch-cases">' + g.cases.map(function (c, ci) { return caseRowHtml(gi, ci, c, g.map, false); }).join("") +
          (g.adding ? '<div class="batch-case"><span class="idx">' + (g.cases.length + 1) + "</span>" +
            projectCasePickHtml("project-case", gi, "選擇要加入的測案…") +
            '<button class="icon-btn" data-act="cancel-add-case" data-g="' + gi + '" title="取消">✕</button></div>'
            : '<button class="batch-add-case" data-act="add-case" data-g="' + gi + '">＋ 新增測案</button>') + "</div>" +
          "</div>";
      }).join("");
    }
    var ready = !!currentProject && batchGroups.length > 0 && batchGroups.every(function (g) { return g.map && g.cases.length; });
    batchRunBtn.disabled = !ready || batchRunning;
    batchSaveToProjectBtn.disabled = !batchGroups.length || !currentProject || batchRunning;
  }
  function addBatchGroup(useCurrentMap) {
    batchGroups.push({ mapName: useCurrentMap ? currentMapName : null, map: useCurrentMap ? currentMapAsFull() : null,
      mapRef: useCurrentMap ? currentMapName : null, setup: null, cases: [] });
    renderBatchGroups();
  }
  batchAddGroupBtn.addEventListener("click", function () {
    if (currentProject) addBatchGroup(!!currentMapName);
  });
  batchGroupsEl.addEventListener("click", function (e) {
    var btn = e.target.closest("button[data-act]");
    if (!btn || batchRunning) return;
    var gi = Number(btn.dataset.g), ci = Number(btn.dataset.c), g = batchGroups[gi];
    var act = btn.dataset.act;
    if (act === "remove-group") batchGroups.splice(gi, 1);
    else if (act === "use-current-map") { g.map = currentMapAsFull(); g.mapName = currentMapName; g.mapRef = currentMapName; }
    else if (act === "add-case") g.adding = true;
    else if (act === "cancel-add-case") g.adding = false;
    else if (act === "remove-case") g.cases.splice(ci, 1);
    else if (act === "remove-setup") g.setup = null;
    else if (act === "case-var-remove") g.cases[ci].vars.splice(Number(btn.dataset.v), 1);
    else if (act === "up" && ci > 0) g.cases.splice(ci - 1, 0, g.cases.splice(ci, 1)[0]);
    else if (act === "down" && ci < g.cases.length - 1) g.cases.splice(ci + 1, 0, g.cases.splice(ci, 1)[0]);
    renderBatchGroups();
    if (act === "add-case") {
      var addPick = batchGroupsEl.querySelector('select[data-act="project-case"][data-g="' + gi + '"]');
      if (addPick) addPick.focus();
    }
  });
  batchGroupsEl.addEventListener("change", async function (e) {
    var pick = e.target.closest("select[data-act]");
    if (pick && pick.dataset.act === "setup-map") {
      var sg = batchGroups[Number(pick.dataset.g)], mapName = pick.value;
      if (!mapName) { sg.setupMapRef = null; sg.setupMap = null; }
      else {
        try { sg.setupMap = await fetchProjectMap(mapName); sg.setupMapRef = mapName; }
        catch (err) { alert("讀取專案地圖失敗：" + err.message); }
      }
      renderBatchGroups();
      return;
    }
    if (pick && (pick.dataset.act === "case-login" || pick.dataset.act === "case-var-add")) {
      var item = batchGroups[Number(pick.dataset.g)].cases[Number(pick.dataset.c)];
      if (pick.dataset.act === "case-login") { if (pick.value) item.login = pick.value; else delete item.login; }
      else if (pick.value) item.vars = (item.vars || []).concat([pick.value]);
      renderBatchGroups();
      return;
    }
    if (pick) {
      var name = pick.value, pg = batchGroups[Number(pick.dataset.g)];
      pick.value = "";
      if (!name) return;
      if (pick.dataset.act === "project-map") {
        try { pg.map = await fetchProjectMap(name); pg.mapRef = name; pg.mapName = name; }
        catch (err) { alert("讀取專案地圖失敗：" + err.message); }
        renderBatchGroups();
        return;
      }
      try {
        var item = { name: name, ref: name, case: await fetchProjectCase(name) };
        if (pick.dataset.act === "project-setup") pg.setup = item; else { pg.cases.push(item); pg.adding = false; }
      } catch (err) { alert("讀取專案測案失敗：" + err.message); }
      renderBatchGroups();
    }
  });
  function batchOptions() {
    return { auto_navigate: batchAutoNavigate.checked, allow_restart: batchAllowRestart.checked, dismiss_dialogs: batchDismissDialogs.checked };
  }
  // 專案裡存的批次設定載入成 batchGroups：整份內嵌的（舊版存法）直接用，
  // 引用專案的（map_ref／ref）去專案抓最新版，找不到的列出來提醒
  // 存檔裡的 login／vars 帶到載入的項目上（account 是舊版的欄位名稱）
  function withVarRefs(target, saved) {
    var login = saved.login || saved.account;
    delete target.account;
    if (login) target.login = login;
    if (saved.vars && saved.vars.length) target.vars = saved.vars.slice();
    return target;
  }
  async function loadBatchConfig(data) {
    var missing = [];
    async function resolveItem(item) {
      if (!item) return null;
      if (!item.ref) return withVarRefs(item, item);
      try {
        return withVarRefs({ name: item.ref, ref: item.ref, case: await fetchProjectCase(item.ref) }, item);
      }
      catch (err) { missing.push(item.ref); return null; }
    }
    var groups = [];
    for (var i = 0; i < data.groups.length; i++) {
      var g = data.groups[i];
      var mapRef = g.map_ref || null;
      if (mapRef && !currentProject) throw new Error("這份批次設定引用專案的地圖，請先開啟專案");
      var refMap = null;
      if (mapRef) {
        try { refMap = await fetchProjectMap(mapRef); }
        catch (err) { throw new Error("第 " + (i + 1) + " 組用的專案地圖「" + mapRef + "」讀不到（可能已改名或刪除）：" + err.message); }
      }
      var cases = [];
      for (var j = 0; j < (g.cases || []).length; j++) {
        var c = await resolveItem(g.cases[j]);
        if (c) cases.push(c);
      }
      var setupMap = null;
      if (g.setup_map_ref) {
        try { setupMap = await fetchProjectMap(g.setup_map_ref); }
        catch (err) { throw new Error("第 " + (i + 1) + " 組的登入地圖「" + g.setup_map_ref + "」讀不到（可能已改名或刪除）：" + err.message); }
      }
      groups.push({ mapName: mapRef || g.map_name, map: mapRef ? refMap : g.map,
        mapRef: mapRef, setup: await resolveItem(g.setup_case), cases: cases,
        setupMapRef: g.setup_map_ref || null, setupMap: setupMap });
    }
    batchGroups = groups;
    var o = data.options || {};
    if (o.auto_navigate !== undefined) batchAutoNavigate.checked = o.auto_navigate;
    if (o.allow_restart !== undefined) batchAllowRestart.checked = o.allow_restart;
    if (o.dismiss_dialogs !== undefined) batchDismissDialogs.checked = o.dismiss_dialogs;
    batchResultsEl.innerHTML = "";
    renderBatchGroups();
    if (missing.length) alert("專案裡找不到這些測案（可能已改名或刪除），已略過：\n" + missing.join("\n"));
  }
  // 執行前把引用專案的地圖／測案換成最新版
  async function refreshBatchRefs() {
    for (var i = 0; i < batchGroups.length; i++) {
      var g = batchGroups[i];
      if (g.mapRef) g.map = await fetchProjectMap(g.mapRef);
      if (g.setupMapRef) g.setupMap = await fetchProjectMap(g.setupMapRef);
      var items = g.cases.concat(g.setup ? [g.setup] : []);
      for (var j = 0; j < items.length; j++) {
        if (items[j].ref) items[j].case = await fetchProjectCase(items[j].ref);
      }
    }
  }

  var BATCH_STATUS_LABEL = { pass: "PASS", fail: "FAIL", skip: "SKIP", running: "執行中", pending: "等待中" };
  function outcomeClass(outcome) {
    return outcome.indexOf("PASS") === 0 ? "pass" : outcome.indexOf("FAIL") === 0 ? "fail" : "skip";
  }
  function renderBatchResults(progress) {
    var all = [];
    progress.groups.forEach(function (g) { all = all.concat(g.cases); });
    var count = function (st) { return all.filter(function (c) { return c.status === st; }).length; };
    var totalSec = all.reduce(function (sum, c) { return sum + (c.duration || 0); }, 0);
    var html = '<div class="compare-summary">' +
      '<div class="summary-card confirmed"><div class="num">' + count("pass") + '</div><div class="label">PASS</div></div>' +
      '<div class="summary-card missing"><div class="num">' + count("fail") + '</div><div class="label">FAIL</div></div>' +
      '<div class="summary-card needs_review"><div class="num">' + count("skip") + '</div><div class="label">SKIP</div></div>' +
      "</div>" +
      '<div class="script-note">共 ' + all.length + " 份測案・已花 " + Math.round(totalSec) + " 秒" +
      (progress.app_id ? "・App：" + escapeHtml(progress.app_id) : "") + (progress.stopped ? "・⏹ 已手動停止" : "") + "</div>";
    progress.groups.forEach(function (g, gi) {
      html += '<div class="section-label">第 ' + (gi + 1) + " 組：" + escapeHtml(g.map_name) +
        (g.setup_name ? "（登入測案：" + escapeHtml(g.setup_name) + "）" : "") + "</div>";
      g.cases.forEach(function (c, ci) {
        var key = gi + "-" + ci;
        var passed = c.results.filter(function (r) { return r.outcome.indexOf("PASS") === 0; }).length;
        var open = batchUserOpen[key] !== undefined ? batchUserOpen[key] : (c.status === "running" || c.status === "fail" || c.status === "skip");
        html += '<details class="batch-result-case" data-key="' + key + '"' + (open ? " open" : "") + ">" +
          '<summary><span class="status-chip ' + c.status + '">' + (BATCH_STATUS_LABEL[c.status] || c.status) + "</span>" +
          '<span class="name">' + escapeHtml(c.test_name || c.name) + "</span>" +
          '<span class="meta">' + (c.login ? "🔑 " + escapeHtml(c.login) + "・" : "") +
          (c.vars && c.vars.length ? "🧩 " + escapeHtml(c.vars.join("、")) + "・" : "") + passed + "/" + c.total + " 步通過" + (c.duration != null ? "・" + c.duration + " 秒" : "") + "</span></summary>" +
          '<div class="batch-result-body">' +
          (c.reason ? '<div class="script-note warn">' + escapeHtml(c.reason) + "</div>" : "") +
          (c.navigation.length ? '<div><b>登入／回到起始畫面</b><ol class="batch-nav-log">' +
            c.navigation.map(function (line) { return "<li>" + escapeHtml(line) + "</li>"; }).join("") + "</ol></div>" : "") +
          (c.precheck ? '<div><b>第0步：</b>' + escapeHtml(c.precheck.message) + "</div>" : "") +
          (c.results.length ? "<div><b>步驟</b>" + c.results.map(function (r) {
            return '<div class="batch-step-line ' + outcomeClass(r.outcome) + '"><span class="no">' + r.index + "</span>" +
              "<span>" + escapeHtml((r.action || "") + (r.note ? "｜" + r.note : "")) + '</span><span class="out">' + escapeHtml(r.outcome) + "</span></div>";
          }).join("") + "</div>" : "") +
          "</div></details>";
      });
    });
    batchResultsEl.innerHTML = html;
  }
  batchResultsEl.addEventListener("click", function (e) {
    var summary = e.target.closest("summary");
    if (!summary) return;
    var details = summary.parentElement;
    batchUserOpen[details.dataset.key] = !details.open;  // click 發生在展開/收合之前
  });
  batchRunBtn.addEventListener("click", async function () {
    try { await refreshBatchRefs(); }
    catch (err) { alert("讀取專案測案失敗（可能已改名或刪除）：" + err.message); return; }
    var total = batchGroups.reduce(function (sum, g) { return sum + g.cases.length; }, 0);
    var warnings = [];
    batchGroups.forEach(function (g, gi) {
      g.cases.concat(g.setup ? [g.setup] : []).forEach(function (c) {
        var missing = caseMissingScreens(c.case, c === g.setup ? setupMapOf(g) : g.map);
        if (missing.length) warnings.push("第 " + (gi + 1) + " 組「" + (c.case.test_name || c.name) + "」：地圖缺少 " + missing.join("、"));
      });
      g.cases.forEach(function (c) {
        var label = "第 " + (gi + 1) + " 組「" + (c.case.test_name || c.name) + "」";
        var files = (c.login ? [c.login] : []).concat(c.vars || []);
        var gone = files.filter(function (n) { return varFileNames().indexOf(n) < 0; });
        if (gone.length) { warnings.push(label + "選的變數檔不存在：" + gone.join("、") + "，會被略過"); return; }
        if (c.login && !g.setup) { warnings.push(label + "選了登入「" + c.login + "」，但這組沒有指定登入測案，會被略過"); return; }
        var uncovered = uncoveredKeys(c.case, files);
        if (c.login && g.setup) uncoveredKeys(g.setup.case, files).forEach(function (k) { if (uncovered.indexOf(k) < 0) uncovered.push(k); });
        if (uncovered.length) warnings.push(label + "選的變數檔裡沒有：" + uncovered.join("、") + "（有設同名環境變數的話可以忽略）");
      });
    });
    var msg = "即將依序執行 " + batchGroups.length + " 組、共 " + total + " 份測案，會實際操作模擬器/裝置" +
      (batchAllowRestart.checked ? "，必要時會重開 App" : "") + "。";
    if (warnings.length) msg += "\n\n⚠️ 以下測案用到的畫面不在地圖裡，那些步驟會失敗：\n" + warnings.join("\n");
    if (!confirm(msg + "\n\n確定要執行嗎？")) return;

    var base = API_BASE;
    batchRunning = true;
    batchUserOpen = {};
    batchRunBtn.disabled = true;
    batchRunBtn.textContent = "執行中...";
    batchStopBtn.hidden = false;
    batchStopBtn.disabled = false;
    batchStopBtn.textContent = "⏹ 停止";
    var timer = null, finished = false, polling = false;
    try {
      for (var i = 0; i < batchGroups.length; i++) await assertPlatformMatches(base, elementMapPlatform(batchGroups[i].map));
      timer = setInterval(async function () {
        if (polling || finished) return;
        polling = true;
        try {
          var pr = await (await fetch(base + "/batch_progress")).json();
          if (pr.running && !finished) {
            var done = 0;
            pr.groups.forEach(function (g) { g.cases.forEach(function (c) { if (["pass", "fail", "skip"].indexOf(c.status) >= 0) done++; }); });
            batchRunBtn.textContent = "執行中（" + done + "/" + total + "）...";
            renderBatchResults(pr);
          }
        } catch (e) { /* 輪詢失敗不影響主流程 */ }
        polling = false;
      }, 1000);
      var res = await fetch(base + "/run_batch", {
        method: "POST", headers: { "Content-Type": "application/json" },
        // 開著專案就用專案設定的 bundle id 重開 App；沒有的話伺服器照舊用 config.toml／前景 App
        body: JSON.stringify({ options: batchOptions(), app_id: (currentProject && currentProject.bundle_id) || null,
          project: currentProject ? currentProject.id : null, groups: batchGroups.map(function (g) {
          return { map_name: g.mapName, map: g.map, setup_case: g.setup, setup_map: g.setupMapRef ? g.setupMap : null, cases: g.cases };
        }) }),
      });
      finished = true;
      var data = await res.json();
      if (data.error) alert("批次執行失敗：" + data.error);
      else renderBatchResults(data);
    } catch (err) {
      alert(failText("連線失敗", err, "（請確認 viewer_server.py 已啟動）"));
    } finally {
      finished = true;
      if (timer) clearInterval(timer);
      batchRunning = false;
      batchRunBtn.textContent = "▶ 執行批次";
      batchStopBtn.hidden = true;
      renderBatchGroups();
    }
  });
  batchStopBtn.addEventListener("click", async function () {
    batchStopBtn.disabled = true;
    batchStopBtn.textContent = "停止中（等目前步驟跑完）...";
    try {
      var data = await (await fetch(API_BASE + "/stop_test_script", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
      })).json();
      if (data.error || !data.stopped) throw new Error(data.error || data.message);
    } catch (err) {
      alert("停止失敗：" + err.message);
      batchStopBtn.disabled = false;
      batchStopBtn.textContent = "⏹ 停止";
    }
  });
  renderBatchGroups();

  // ================= 專案（workspace）=================
  // 地圖、截圖、測案、批次都存在伺服器的 workspace/projects/<專案>/ 底下：
  // 地圖改了自動存檔，測案／批次按「💾 存到專案」存，重新整理或換台電腦
  // （拿到專案 zip）都還在。沒開專案時照舊用檔案載入／匯出。
  var projectSelect = document.getElementById("projectSelect");
  var projectSaveStatus = document.getElementById("projectSaveStatus");
  var projectExportBtn = document.getElementById("projectExportBtn");
  var llmgenCaseProjectSelect = document.getElementById("llmgenCaseProjectSelect");
  var runscriptCaseProjectSelect = document.getElementById("runscriptCaseProjectSelect");
  var batchProjectSelect = document.getElementById("batchProjectSelect");
  var mapSaveTimer = null, mapSaving = false, mapDirty = false, mapConflict = false;

  async function apiJson(method, path, body) {
    var opts = { method: method, headers: {} };
    if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
    var res = await fetch(API_BASE + path, opts);
    var data = await res.json().catch(function () { return {}; });
    if (!res.ok || data.error) {
      var err = new Error(data.error || ("HTTP " + res.status));
      err.status = res.status;
      throw err;
    }
    return data;
  }
  function projectApi(sub) { return "/api/projects/" + encodeURIComponent(currentProject.id) + (sub || ""); }
  async function fetchProjectCase(name) {
    if (!currentProject) throw new Error("沒有開啟專案");
    return (await apiJson("GET", projectApi("/cases/" + encodeURIComponent(name)))).data;
  }

  function setProjectStatus(text, warn, actions) {
    projectSaveStatus.className = "project-save-status" + (warn ? " warn" : "");
    projectSaveStatus.textContent = text;
    (actions || []).forEach(function (a) {
      var btn = document.createElement("button");
      btn.className = "btn";
      btn.textContent = a.label;
      btn.addEventListener("click", a.fn);
      projectSaveStatus.appendChild(btn);
    });
  }

  // 地圖每次修改都呼叫：停手 0.8 秒後存一次，連續操作只存最後一版
  function markMapDirty() {
    if (!currentProject) return;
    mapDirty = true;
    if (mapConflict) return;
    setProjectStatus("有修改，準備存檔…");
    clearTimeout(mapSaveTimer);
    mapSaveTimer = setTimeout(saveMapNow, 800);
  }
  async function saveMapNow() {
    clearTimeout(mapSaveTimer);
    if (!currentProject || !mapDirty || mapConflict) return;
    if (mapSaving) { mapSaveTimer = setTimeout(saveMapNow, 300); return; }
    var project = currentProject, mapName = currentMapName, failed = false;
    function switched() { return project !== currentProject || mapName !== currentMapName; }
    mapSaving = true;
    mapDirty = false;
    setProjectStatus("儲存中…");
    try {
      var data = await apiJson("PUT", mapApi(mapName), { map: currentMapAsFull(), base_version: mapVersion });
      if (switched()) return;
      mapVersion = data.map_version;
      projectMaps.forEach(function (m) { if (m.name === mapName) m.screens = screens.length; });
      renderMapPicker();
      if (!mapDirty) setProjectStatus("已自動儲存 " + new Date().toLocaleTimeString());
    } catch (err) {
      if (switched()) return;
      failed = true;
      mapDirty = true;
      if (err.status === 409) {
        mapConflict = true;
        setProjectStatus("⚠️ " + err.message + "。這次的修改還沒存進專案，需要的話先「⬇️ 匯出目前地圖」留一份。", true,
          [{ label: "重新載入地圖（放棄這次修改）", fn: function () { openMap(mapName, true); } }]);
      } else {
        setProjectStatus("⚠️ 存檔失敗：" + err.message, true, [{ label: "重試", fn: saveMapNow }]);
      }
    } finally {
      mapSaving = false;
      // 存檔途中又改了：再存一次（失敗的話等使用者按重試或下一次修改）
      if (!failed && mapDirty && !switched()) mapSaveTimer = setTimeout(saveMapNow, 300);
    }
  }
  // 切換／匯出專案前先把還沒存的地圖存掉；回傳 false 代表還有存不進去的修改
  async function flushMapSave() {
    while (mapSaving) await new Promise(function (r) { setTimeout(r, 100); });
    if (mapDirty && !mapConflict) await saveMapNow();
    return !mapDirty;
  }
  window.addEventListener("beforeunload", function (e) {
    if (mapDirty || mapSaving) { e.preventDefault(); e.returnValue = ""; }
  });

  async function loadProjectList() {
    projectList = (await apiJson("GET", "/api/projects")).projects;
    projectSelect.innerHTML = '<option value="">（不使用專案：手動載入／匯出檔案）</option>' +
      projectList.map(function (p) {
        return '<option value="' + escapeHtml(p.id) + '">' + escapeHtml(p.name + "（" + platformName(p.platform) + "・" + p.id + "）") + "</option>";
      }).join("");
    projectSelect.value = currentProject ? currentProject.id : "";
  }

  async function openProject(id) {
    if (currentProject && !(await flushMapSave()) &&
        !confirm("專案「" + currentProject.name + "」的地圖還有修改沒存進去，切換會丟掉這些修改，確定嗎？")) {
      projectSelect.value = currentProject.id;
      return;
    }
    var d = null;
    if (id) {
      try { d = await apiJson("GET", "/api/projects/" + encodeURIComponent(id)); }
      catch (err) {
        alert("開啟專案失敗：" + err.message);
        projectSelect.value = currentProject ? currentProject.id : "";
        return;
      }
    }
    currentProject = d ? d.project : null;
    projectMaps = d ? d.maps : [];
    projectCases = d ? d.cases : [];
    projectBatches = d ? d.batches : [];
    projectVars = d ? d.vars || [] : [];
    runscriptVars = [];
    currentMapName = null; mapVersion = null;
    mapDirty = false; mapConflict = false;
    batchGroups = [];  // 批次設定引用的是上一個專案的地圖／測案，換專案就清掉
    batchResultsEl.innerHTML = "";
    try { localStorage.setItem("lastProject", id || ""); } catch (e) { /* 存不了就下次手動選 */ }
    projectSelect.value = id || "";
    if (!d) {
      applyMapData({ app: {}, screens: [], edges: [], skipped: [] });
      updateProjectUi();
      setProjectStatus("沒有使用專案：地圖、測案改了要自己匯出存檔");
      return;
    }
    // 開上次在這個專案開的地圖，沒有的話開 main，再沒有就開第一張
    var names = projectMaps.map(function (m) { return m.name; }), lastMap = null;
    try { lastMap = localStorage.getItem("lastMap:" + id); } catch (e) { /* 照預設 */ }
    var mapName = names.indexOf(lastMap) >= 0 ? lastMap : names.indexOf("main") >= 0 ? "main" : names[0];
    if (!(await openMap(mapName, true))) { updateProjectUi(); return; }
    try {
      var serverPlatform = await fetchServerPlatform(API_BASE);
      if (serverPlatform && serverPlatform !== currentProject.platform) {
        setProjectStatus("⚠️ 專案是 " + platformName(currentProject.platform) + "，但 viewer_server 目前接的是 " +
          platformName(serverPlatform) + "：可以看地圖、編輯測案，但操作手機會被擋下來", true);
      }
    } catch (e) { /* 只是提醒，連不上不影響開專案 */ }
  }

  // ---- 專案裡的多張地圖 ----
  function mapApi(name) { return projectApi("/maps/" + encodeURIComponent(name)); }
  // 批次用：目前開著的地圖拿記憶體裡的（可能還沒存檔），其他張去伺服器讀
  async function fetchProjectMap(name) {
    if (!currentProject) throw new Error("沒有開啟專案");
    if (name === currentMapName) return currentMapAsFull();
    return (await apiJson("GET", mapApi(name))).map;
  }
  // 切到專案裡的另一張地圖。discard=true 代表呼叫端已經處理過還沒存的修改。回傳 true＝有開成
  async function openMap(name, discard) {
    if (!discard && !(await flushMapSave()) &&
        !confirm("地圖「" + currentMapName + "」還有修改沒存進去，切換會丟掉這些修改，確定嗎？")) {
      renderMapPicker();
      return false;
    }
    var d;
    try { d = await apiJson("GET", mapApi(name)); }
    catch (err) {
      alert("開啟地圖「" + name + "」失敗：" + err.message);
      renderMapPicker();
      return false;
    }
    currentMapName = name;
    mapVersion = d.map_version;
    mapDirty = false; mapConflict = false;
    clearTimeout(mapSaveTimer);
    try { localStorage.setItem("lastMap:" + currentProject.id, name); } catch (e) { /* 下次開 main */ }
    var map = d.map;
    map.app = map.app || {};
    if (!map.app.platform) map.app.platform = currentProject.platform;
    applyMapData(map);
    updateProjectUi();
    setProjectStatus("已開啟地圖「" + name + "」（" + screens.length + " 個畫面）・專案共 " + projectMaps.length + " 張地圖、" + projectCases.length + " 份測案");
    return true;
  }
  function renderMapPicker() {
    var on = !!currentProject;
    document.getElementById("mapControls").hidden = !on;
    var mapSelect = document.getElementById("mapSelect");
    mapSelect.innerHTML = projectMaps.map(function (m) {
      return '<option value="' + escapeHtml(m.name) + '">' + escapeHtml(m.name + (m.screens != null ? "（" + m.screens + " 個畫面）" : "")) + "</option>";
    }).join("");
    mapSelect.value = currentMapName || "";
    if (!mapManagerModal.hidden) renderMapManager();
  }
  // 新增一張地圖（map 沒給就是空白）並切過去。失敗會 throw，讓呼叫端決定錯誤顯示在哪裡
  async function createProjectMap(name, map) {
    if (!(await flushMapSave()) && !confirm("地圖「" + currentMapName + "」還有修改沒存進去，切換會丟掉這些修改，確定嗎？")) return false;
    var res = await apiJson("POST", mapApi(name), map ? { map: map } : {});
    projectMaps = res.maps;
    await openMap(name, true);
    return true;
  }
  document.getElementById("mapSelect").addEventListener("change", function (e) { openMap(e.target.value); });

  // ---- 管理地圖視窗：列出所有地圖，每一列直接改名／複製／刪除，不跳瀏覽器對話框 ----
  var mapManagerModal = document.getElementById("mapManagerModal");
  var mapManagerRows = document.getElementById("mapManagerRows");
  var mapManagerMsg = document.getElementById("mapManagerMsg");
  // 正在編輯的那一列：{ name, mode: "rename" | "copy" | "delete" }，同時只會有一列在編輯
  var mapRowEdit = null;
  var mapManagerBusy = false;

  function setMapManagerMsg(text, isError) {
    mapManagerMsg.textContent = text || "";
    mapManagerMsg.className = "map-manager-msg" + (isError ? " error" : "");
  }
  function shortTime(t) { return t ? t.slice(5, 16).replace("-", "/") : ""; }  // 2026-10-07 14:20:33 → 10/07 14:20
  // 名稱的基本檢查（空白、撞名）；格式不合法交給伺服器回錯誤
  function mapNameProblem(name, exceptName) {
    if (!name) return "請輸入名稱";
    if (name !== exceptName && projectMaps.some(function (m) { return m.name === name; })) return "已經有地圖叫「" + name + "」了，換一個名稱";
    return null;
  }

  function renderMapManager() {
    document.getElementById("mapManagerTitle").textContent = "管理地圖：" + (currentProject ? currentProject.name : "");
    document.getElementById("mapManagerProjectId").textContent = currentProject ? currentProject.id : "";
    var onlyOne = projectMaps.length <= 1;
    mapManagerRows.innerHTML = projectMaps.map(function (m) {
      var name = escapeHtml(m.name), isCurrent = m.name === currentMapName;
      var edit = mapRowEdit && mapRowEdit.name === m.name ? mapRowEdit.mode : null;
      var first, actions;
      if (edit === "rename") {
        first = '<input type="text" data-row-input value="' + name + '" aria-label="新名稱">';
        actions = '<button class="btn primary" data-act="save">✓ 改名</button><button class="btn" data-act="cancel">取消</button>';
      } else if (edit === "delete") {
        first = '<span class="map-row-confirm">確定刪除「' + name + '」？無法復原</span>';
        actions = '<button class="btn danger" data-act="confirm-delete">刪除</button><button class="btn" data-act="cancel">取消</button>';
      } else {
        first = name + (isCurrent ? '<span class="map-current-badge">目前</span>' : "");
        actions = (isCurrent ? "" : '<button class="btn" data-act="open">開啟</button>') +
          '<button class="btn" data-act="rename">改名</button>' +
          '<button class="btn" data-act="copy">複製</button>' +
          '<button class="btn danger" data-act="delete"' + (onlyOne ? ' disabled title="專案最後一張地圖不能刪"' : "") + ">刪除</button>";
      }
      var html = '<tr data-name="' + name + '"' + (isCurrent ? ' class="current"' : "") + ">" +
        "<td>" + first + '</td><td class="num">' + (m.screens != null ? m.screens : "?") + "</td>" +
        '<td class="time">' + escapeHtml(shortTime(m.updated_at)) + '</td><td class="actions">' + actions + "</td></tr>";
      // 複製：原本那一列不動，在下面多一列輸入複本名稱，看得出來是「多一張」不是改原本的名稱
      if (edit === "copy") {
        html += '<tr class="map-copy-row" data-name="' + name + '">' +
          '<td><div class="map-copy-cell"><span class="map-copy-label">↳ 複本名稱</span><input type="text" data-row-input value="' + escapeHtml(m.name + "_copy") + '" aria-label="複本名稱"></div></td>' +
          '<td class="num">' + (m.screens != null ? m.screens : "?") + '</td><td class="time">新的</td>' +
          '<td class="actions"><button class="btn primary" data-act="save">✓ 建立複本</button><button class="btn" data-act="cancel">取消</button></td></tr>';
      }
      return html;
    }).join("");
    var input = mapManagerRows.querySelector("[data-row-input]");
    if (input) { input.focus(); input.select(); }
  }

  async function openMapManager() {
    if (!currentProject) return;
    mapRowEdit = null;
    setMapManagerMsg("");
    document.getElementById("mapNewName").value = "";
    mapManagerModal.hidden = false;
    renderMapManager();
    // 重新抓一次清單：畫面數、更新時間可能在別的分頁或爬蟲那邊變了
    try {
      projectMaps = (await apiJson("GET", projectApi())).maps;
      renderMapPicker();
    } catch (err) { setMapManagerMsg("更新地圖清單失敗：" + err.message, true); }
  }
  function closeMapManager() { mapManagerModal.hidden = true; mapRowEdit = null; }

  async function runMapAction(name, act, input) {
    if (act === "cancel") { mapRowEdit = null; setMapManagerMsg(""); renderMapManager(); return; }
    if (act === "rename" || act === "copy" || act === "delete") { mapRowEdit = { name: name, mode: act }; setMapManagerMsg(""); renderMapManager(); return; }
    if (mapManagerBusy) return;
    mapManagerBusy = true;
    try {
      if (act === "open") {
        if (await openMap(name)) closeMapManager();
      } else if (act === "save" && mapRowEdit.mode === "rename") {
        var newName = (input.value || "").trim();
        var problem = mapNameProblem(newName, name);
        if (problem) { setMapManagerMsg(problem, true); return; }
        if (newName === name) { mapRowEdit = null; renderMapManager(); return; }
        if (name === currentMapName && !(await flushMapSave())) { setMapManagerMsg("目前地圖還有修改沒存進去，請先處理存檔問題再改名", true); return; }
        var res = await apiJson("POST", mapApi(name) + "/rename", { new_name: newName });
        projectMaps = res.maps;
        batchGroups.forEach(function (g) { if (g.mapRef === name) { g.mapRef = newName; g.mapName = newName; } });
        if (name === currentMapName) {
          currentMapName = newName;
          try { localStorage.setItem("lastMap:" + currentProject.id, newName); } catch (e) { /* 無所謂 */ }
        }
        mapRowEdit = null;
        updateProjectUi();
        setMapManagerMsg("已改名為「" + newName + "」（專案裡存的批次設定也一起改了）");
      } else if (act === "save" && mapRowEdit.mode === "copy") {
        var copyName = (input.value || "").trim();
        var copyProblem = mapNameProblem(copyName);
        if (copyProblem) { setMapManagerMsg(copyProblem, true); return; }
        // 目前這張用記憶體裡的（包含還沒存檔的修改），其他張去伺服器讀
        if (await createProjectMap(copyName, await fetchProjectMap(name))) {
          mapRowEdit = null;
          renderMapManager();
          setMapManagerMsg("已複製「" + name + "」成「" + copyName + "」，並切換過去");
        }
      } else if (act === "confirm-delete") {
        var del = await apiJson("DELETE", mapApi(name));
        projectMaps = del.maps;
        mapRowEdit = null;
        if (name === currentMapName) {
          var names = projectMaps.map(function (m) { return m.name; });
          mapDirty = false;  // 刪掉的地圖不用再存
          await openMap(names.indexOf("main") >= 0 ? "main" : names[0], true);
        } else {
          renderMapPicker();
        }
        renderMapManager();
        setMapManagerMsg("已刪除「" + name + "」。截圖不會刪，之後可以用 tools/cleanup_images.py --project 清掉沒人用的");
      }
    } catch (err) {
      setMapManagerMsg("失敗：" + err.message, true);
    } finally {
      mapManagerBusy = false;
    }
  }

  document.getElementById("mapManageBtn").addEventListener("click", openMapManager);
  document.getElementById("mapManagerCloseBtn").addEventListener("click", closeMapManager);
  mapManagerModal.addEventListener("click", function (e) { if (e.target === mapManagerModal) closeMapManager(); });
  mapManagerRows.addEventListener("click", function (e) {
    var btn = e.target.closest("button[data-act]");
    if (!btn) return;
    var row = btn.closest("tr");
    runMapAction(row.dataset.name, btn.dataset.act, row.querySelector("[data-row-input]"));
  });
  mapManagerRows.addEventListener("keydown", function (e) {
    var input = e.target.closest("[data-row-input]");
    if (!input) return;
    if (e.key === "Enter") { e.preventDefault(); runMapAction(input.closest("tr").dataset.name, "save", input); }
    if (e.key === "Escape") { e.stopPropagation(); runMapAction(null, "cancel"); }
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && !mapManagerModal.hidden) closeMapManager();
  });
  document.getElementById("mapNewForm").addEventListener("submit", async function (e) {
    e.preventDefault();
    var input = document.getElementById("mapNewName");
    var name = (input.value || "").trim();
    var problem = mapNameProblem(name);
    if (problem) { setMapManagerMsg(problem, true); return; }
    if (mapManagerBusy) return;
    mapManagerBusy = true;
    try {
      if (await createProjectMap(name, null)) {
        input.value = "";
        mapRowEdit = null;
        renderMapManager();
        setMapManagerMsg("已新增「" + name + "」並切換過去，關掉視窗就可以開始手動建樹");
      }
    } catch (err) {
      setMapManagerMsg("新增失敗：" + err.message, true);
    } finally {
      mapManagerBusy = false;
    }
  });
  // 批次每一組裡的「選專案地圖」下拉選單
  function projectMapPickHtml(gi) {
    if (!currentProject) return "";
    return '<select class="project-pick" data-act="project-map" data-g="' + gi + '"><option value="">📁 選專案地圖…</option>' +
      projectMaps.map(function (m) { return '<option value="' + escapeHtml(m.name) + '">' + escapeHtml(m.name) + "</option>"; }).join("") + "</select>";
  }

  // extraHtml：放在提示文字後面、專案測案前面的額外選項
  function caseOptionsHtml(placeholder, extraHtml) {
    return '<option value="">' + escapeHtml(placeholder + (projectCases.length || !currentProject ? "" : "（專案還沒有測案）")) + "</option>" + (extraHtml || "") +
      projectCases.map(function (c) {
        var label = c.name + (c.test_name && c.test_name !== c.name ? "（" + c.test_name + "）" : "") + (c.steps != null ? "・" + c.steps + " 步" : "");
        return '<option value="' + escapeHtml(c.name) + '">' + escapeHtml(label) + "</option>";
      }).join("");
  }
  // 批次每一組裡的「從專案選測案」下拉選單（沒開專案就不顯示）
  function projectCasePickHtml(act, gi, placeholder) {
    if (!currentProject) return "";
    return '<select class="project-pick" data-act="' + act + '" data-g="' + gi + '">' + caseOptionsHtml(placeholder) + "</select>";
  }
  function renderProjectPickers() {
    var on = !!currentProject;
    llmgenCaseProjectSelect.hidden = !on;
    llmgenCaseProjectSelect.innerHTML = caseOptionsHtml("📁 開啟專案測案…");
    llmgenCaseProjectSelect.value = llmgenProjectName || "";
    renderRunscriptCasePicker();
    llmgenSaveToProjectBtn.hidden = !on;
    batchSaveToProjectBtn.hidden = !on;
    document.getElementById("varsManageBtn").hidden = !on;
    llmgenVarsCheckBtn.hidden = !on;
    llmgenVarsCheckBtn.disabled = !on || !llmgenLatestCase;
    renderRunscriptVars();
    batchProjectSelect.hidden = !on;
    batchProjectSelect.innerHTML = '<option value="">' + (projectBatches.length ? "📁 開啟專案裡的批次…" : "📁 專案還沒有存批次") + "</option>" +
      projectBatches.map(function (b) { return '<option value="' + escapeHtml(b.name) + '">' + escapeHtml(b.name) + "</option>"; }).join("");
  }
  function updateProjectUi() {
    var on = !!currentProject;
    document.getElementById("mapTitle").textContent = on ? currentProject.name + " 畫面地圖" : "畫面地圖";
    document.title = on ? currentProject.name + "｜畫面地圖" : "畫面地圖";
    projectExportBtn.disabled = !on;
    document.getElementById("importMapLabel").firstChild.textContent = on ? "📂 匯入地圖（zip／json）" : "📂 載入之前匯出的地圖";
    document.getElementById("exportMapBtn").textContent = on ? "⬇️ 匯出目前地圖（zip，含截圖）" : "⬇️ 匯出目前地圖（含手動新增的節點）";
    renderMapPicker();
    renderProjectPickers();
    updateLlmgenCaseControls();
    renderBatchGroups();
  }

  // 存一份測案／批次到專案；同名已存在會先問要不要覆蓋。回傳 true＝有存
  // overwrite＝存回原本開啟的那一份，同名直接覆蓋不用問
  async function saveProjectItem(kind, name, data, overwrite) {
    var path = projectApi("/" + kind + "/" + encodeURIComponent(name));
    var res;
    try { res = await apiJson("PUT", path + (overwrite ? "?overwrite=1" : ""), data); }
    catch (err) {
      if (err.status !== 409) throw err;
      if (!confirm("專案裡已經有「" + name + "」，要覆蓋嗎？")) return false;
      res = await apiJson("PUT", path + "?overwrite=1", data);
    }
    if (kind === "cases") projectCases = res.items; else projectBatches = res.items;
    renderProjectPickers();
    renderBatchGroups();
    return true;
  }

  projectSelect.addEventListener("change", function () { openProject(projectSelect.value); });

  projectExportBtn.addEventListener("click", async function () {
    if (!currentProject) return;
    if (!(await flushMapSave()) && !confirm("地圖還有修改沒存進專案，匯出的 zip 不會包含這些修改，還是要匯出嗎？")) return;
    var a = document.createElement("a");
    a.href = API_BASE + projectApi("/export");
    a.download = currentProject.id + ".zip";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  });

  async function postZip(path, file) {
    var res = await fetch(API_BASE + path, { method: "POST", headers: { "Content-Type": "application/zip" }, body: file });
    var data = await res.json().catch(function () { return {}; });
    return { ok: res.ok && !data.error, status: res.status, data: data };
  }

  document.getElementById("projectImportInput").addEventListener("change", async function (e) {
    var file = e.target.files[0]; e.target.value = "";
    if (!file) return;
    var newId = "";
    for (;;) {
      var r = await postZip("/api/projects/import" + (newId ? "?id=" + encodeURIComponent(newId) : ""), file);
      if (r.ok) {
        await loadProjectList();
        await openProject(r.data.project.id);
        return;
      }
      if (r.status !== 409) { alert("匯入專案失敗：" + (r.data.error || ("HTTP " + r.status))); return; }
      var message = r.data.error || "專案已經存在";
      if (!newId) {
        // 本機已經有同代號的專案：先看能不能合併（兩個人分頭編同一個專案、互相同步）
        var preview = await postZip("/api/projects/merge?preview=1", file);
        if (preview.ok) {
          var choice = await askMerge(preview.data);
          if (!choice) return;
          if (choice.action === "merge") { await doMerge(file, choice.resolutions); return; }
        } else {
          message += "\n（也不能合併：" + (preview.data.error || ("HTTP " + preview.status)) + "）";
        }
      }
      newId = (prompt(message + "\n\n換一個專案代號匯入（留空取消）：", "") || "").trim();
      if (!newId) return;
    }
  });

  // ---- 合併匯入視窗：列出會新增什麼、同名衝突逐一選怎麼處理 ----
  var mergeModal = document.getElementById("mergeModal");
  var mergeResolve = null;
  var KIND_LABELS = { maps: "地圖", cases: "測案", batches: "批次" };

  function closeMerge(result) {
    mergeModal.hidden = true;
    if (mergeResolve) { var r = mergeResolve; mergeResolve = null; r(result); }
  }
  // 回傳 null（取消）、{action: "rename"}、或 {action: "merge", resolutions}
  function askMerge(preview) {
    var counts = {};
    preview.new.forEach(function (i) { counts[i.kind] = (counts[i.kind] || 0) + 1; });
    var parts = Object.keys(KIND_LABELS).filter(function (k) { return counts[k]; })
      .map(function (k) { return counts[k] + " " + (k === "maps" ? "張" : "份") + KIND_LABELS[k]; });
    if (preview.screenshots) parts.push(preview.screenshots + " 張截圖");
    document.getElementById("mergeTitle").textContent = "專案「" + preview.project.name + "」已經存在：合併進去？";
    document.getElementById("mergeSummary").textContent = parts.length
      ? "會新增：" + parts.join("、") + "（" + preview.new.map(function (i) { return i.name; }).join("、") + "）"
      : "沒有新的地圖、測案、批次或截圖要加進來。";
    document.getElementById("mergeConflictBox").hidden = !preview.conflicts.length;
    document.getElementById("mergeConflictRows").innerHTML = preview.conflicts.map(function (c) {
      return "<tr><td>" + KIND_LABELS[c.kind] + "</td><td>" + escapeHtml(c.name) + "</td><td>" +
        '<select data-key="' + escapeHtml(c.kind + "/" + c.name) + '">' +
        '<option value="mine">保留我的</option>' +
        '<option value="theirs">用匯入的</option>' +
        '<option value="both">兩個都留（匯入的改名）</option>' +
        "</select></td></tr>";
    }).join("");
    mergeModal.hidden = false;
    return new Promise(function (resolve) { mergeResolve = resolve; });
  }
  document.getElementById("mergeCloseBtn").addEventListener("click", function () { closeMerge(null); });
  document.getElementById("mergeRenameBtn").addEventListener("click", function () { closeMerge({ action: "rename" }); });
  document.getElementById("mergeConfirmBtn").addEventListener("click", function () {
    var resolutions = {};
    mergeModal.querySelectorAll("select[data-key]").forEach(function (s) { resolutions[s.dataset.key] = s.value; });
    closeMerge({ action: "merge", resolutions: resolutions });
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && !mergeModal.hidden) closeMerge(null); });

  async function doMerge(file, resolutions) {
    // 合併可能改到目前開著的地圖：先把還沒存的存掉，合併完再重新開專案讀最新的
    if (currentProject && !(await flushMapSave()) &&
        !confirm("地圖「" + currentMapName + "」還有修改沒存進去，合併後重新載入會丟掉這些修改，確定嗎？")) return;
    var r = await postZip("/api/projects/merge?resolutions=" + encodeURIComponent(JSON.stringify(resolutions)), file);
    if (!r.ok) { alert("合併失敗：" + (r.data.error || ("HTTP " + r.status))); return; }
    var d = r.data, lines = [];
    function names(list) { return list.map(function (i) { return KIND_LABELS[i.kind] + "「" + i.name + "」"; }).join("、"); }
    if (d.added.length) lines.push("新增：" + names(d.added));
    if (d.replaced.length) lines.push("換成匯入的：" + names(d.replaced));
    if (d.renamed.length) lines.push("兩個都留：" + d.renamed.map(function (i) { return KIND_LABELS[i.kind] + "「" + i.name + "」→「" + i.as + "」"; }).join("、"));
    if (d.screenshots) lines.push("補進 " + d.screenshots + " 張截圖");
    if (d.backups.length) lines.push("被換掉的地圖已備份到 backups/");
    mapDirty = false;  // 上面已經存過或使用者同意丟掉；重新開專案時不要再問一次
    await loadProjectList();
    await openProject(d.project.id);
    alert("合併完成" + (lines.length ? "\n\n" + lines.join("\n") : "：沒有需要更新的東西"));
  }

  llmgenCaseProjectSelect.addEventListener("change", async function () {
    var name = llmgenCaseProjectSelect.value;
    if (!name) { renderLlmgenCaseStatus(); return; }
    try { importLlmgenCase(await fetchProjectCase(name), "📁 開啟專案測案「" + name + "」", { projectName: name }); }
    catch (err) { alert("讀取專案測案失敗：" + err.message); }
    renderLlmgenCaseStatus();
  });
  llmgenSaveToProjectBtn.addEventListener("click", async function () {
    if (!llmgenLatestCase || !currentProject) return;
    var defaultName = llmgenProjectName || (llmgenLatestCase.test_name || "llm_test_case").replace(/[^\w一-鿿-]+/g, "_");
    var name = (prompt("存成專案測案，名稱（也是檔名；跟原本同名就是覆蓋）：", defaultName) || "").trim();
    if (!name) return;
    try {
      if (!(await saveProjectItem("cases", name, llmgenLatestCase, name === llmgenProjectName))) return;
      llmgenProjectName = name;
      llmgenFileName = null;
      llmgenSavedText = JSON.stringify(llmgenLatestCase);
      renderLlmgenCaseStatus();
      setProjectStatus("測案「" + name + "」已存進專案");
    } catch (err) { alert("存檔失敗：" + err.message); }
  });
  runscriptCaseProjectSelect.addEventListener("change", async function () {
    var name = runscriptCaseProjectSelect.value;
    if (!name || name === "__editor__") { renderRunscriptCasePicker(); return; }
    try {
      runscriptTestCase = await fetchProjectCase(name);
      runscriptCaseRef = name;
      renderRunscriptPreview();
    } catch (err) {
      alert("讀取專案測案失敗：" + err.message);
      renderRunscriptCasePicker();
    }
  });

  // ---- 變數檔（vars/）：測案裡的 ${KEY} 執行時換成選的變數檔的值 ----
  var VAR_RE = /\$\{([A-Za-z_][A-Za-z0-9_]*)\}/g;
  var SECRET_KEY_RE = /PASS|PWD|PIN|SECRET|TOKEN/i;
  function varFileNames() { return projectVars.map(function (v) { return v.name; }); }
  // 測案用到的變數 KEY（step.value 是字串或跑馬燈的字串陣列）
  function caseVarKeys(testCase) {
    var keys = [];
    ((testCase && testCase.steps) || []).forEach(function (st) {
      [].concat(st.value || []).forEach(function (text) {
        String(text).replace(VAR_RE, function (m, k) { if (keys.indexOf(k) < 0) keys.push(k); return m; });
      });
    });
    return keys;
  }
  // 測案用到、但這些變數檔裡都沒有的 KEY
  function uncoveredKeys(testCase, fileNames) {
    var have = {};
    projectVars.forEach(function (v) { if (fileNames.indexOf(v.name) >= 0) (v.keys || []).forEach(function (k) { have[k] = true; }); });
    return caseVarKeys(testCase).filter(function (k) { return !have[k]; });
  }
  // 變數檔的 chip ＋「＋ 變數」選單；attrs 是放進按鈕／選單的 data-*，act 是事件名稱前綴
  function varsPickHtml(selected, attrs, act) {
    var names = varFileNames();
    selected = selected || [];
    return '<span class="vars-pick">' + selected.map(function (n, i) {
      var gone = names.indexOf(n) < 0;
      return '<span class="vars-chip' + (gone ? " missing" : "") + '" title="' + (gone ? "變數檔裡沒有這個（可能已改名或刪除）" : "變數檔") + '">🧩 ' + escapeHtml(n) +
        '<button type="button" data-act="' + act + '-remove" data-v="' + i + '" ' + attrs + ' title="移除">✕</button></span>';
    }).join("") +
      '<select class="project-pick" data-act="' + act + '-add" ' + attrs + ' title="另外要代入的變數檔（股票代號、張數…），同名的以後面的為準">' +
      '<option value="">＋ 變數</option>' + names.filter(function (n) { return selected.indexOf(n) < 0; }).map(function (n) {
        return '<option value="' + escapeHtml(n) + '">' + escapeHtml(n) + "</option>";
      }).join("") + "</select></span>";
  }
  function setProjectVars(items) {
    projectVars = items || [];
    renderBatchGroups();
    renderRunscriptVars();
  }

  // 單份執行的變數檔
  function renderRunscriptVars() {
    document.getElementById("runscriptVarsRow").hidden = !currentProject;
    runscriptVarsPick.innerHTML = currentProject ? varsPickHtml(runscriptVars, "", "run-var") : "";
  }
  runscriptVarsPick.addEventListener("click", function (e) {
    var btn = e.target.closest('button[data-act="run-var-remove"]');
    if (!btn) return;
    runscriptVars.splice(Number(btn.dataset.v), 1);
    renderRunscriptVars();
  });
  runscriptVarsPick.addEventListener("change", function (e) {
    var pick = e.target.closest('select[data-act="run-var-add"]');
    if (!pick || !pick.value) return;
    runscriptVars.push(pick.value);
    renderRunscriptVars();
  });

  // 變數管理視窗：左邊檔案清單，右邊編輯目前這個檔的 KEY / 值
  var varsModal = document.getElementById("varsModal");
  var varsFileList = document.getElementById("varsFileList");
  var varsEditor = document.getElementById("varsEditor");
  var varsMsg = document.getElementById("varsMsg");
  // varsDraft：{ name, sensitive, rows: [{k, v, show}] }，dirty＝有改還沒存
  var varsDraft = null, varsDirty = false;
  function varsApi(name, sub) { return projectApi("/vars/" + encodeURIComponent(name) + (sub || "")); }
  function setVarsMsg(text, isError) {
    varsMsg.textContent = text || "";
    varsMsg.className = "map-manager-msg" + (isError ? " error" : "");
  }
  function renderVarsFiles() {
    varsFileList.innerHTML = projectVars.length ? projectVars.map(function (v) {
      var cur = varsDraft && varsDraft.name === v.name;
      return '<button type="button" data-name="' + escapeHtml(v.name) + '"' + (cur ? ' class="current"' : "") + ">" +
        (v.sensitive ? "🔒 " : "") + escapeHtml(v.name) + '<span class="keys">' + escapeHtml((v.keys || []).join(", ") || "（沒有變數）") + "</span></button>";
    }).join("") : '<div class="map-manager-hint">還沒有變數檔，在下面輸入名稱新增</div>';
  }
  function renderVarsEditor() {
    if (!varsDraft) { varsEditor.innerHTML = '<div class="map-manager-hint">選左邊的變數檔來編輯</div>'; return; }
    varsEditor.innerHTML =
      '<div class="vars-editor-head"><h4>' + escapeHtml(varsDraft.name) + (varsDirty ? "（有修改還沒存）" : "") + "</h4>" +
      '<button class="btn" data-act="rename">改名</button><button class="btn" data-act="copy">複製</button>' +
      '<button class="btn danger" data-act="delete">刪除</button></div>' +
      '<label class="field-row" style="gap:4px;"><input type="checkbox" data-act="sensitive"' + (varsDraft.sensitive ? " checked" : "") +
      "> 敏感（帳密這類，匯出專案 zip 時不帶出去）</label>" +
      '<table class="map-table"><thead><tr><th style="width:40%">KEY（測案裡寫 ${KEY}）</th><th>值</th><th></th></tr></thead><tbody>' +
      varsDraft.rows.map(function (r, i) {
        var secret = SECRET_KEY_RE.test(r.k) && !r.show;
        return '<tr><td><input class="vars-input" data-row="' + i + '" data-field="k" value="' + escapeHtml(r.k) + '" placeholder="例如 STOCK_CODE" autocomplete="off"></td>' +
          '<td><div class="vars-value-cell"><input class="vars-input" data-row="' + i + '" data-field="v" type="' + (secret ? "password" : "text") + '" value="' + escapeHtml(r.v) + '" autocomplete="off">' +
          (SECRET_KEY_RE.test(r.k) ? '<button class="icon-btn" data-act="show" data-row="' + i + '" title="顯示／隱藏">👁</button>' : "") + "</div></td>" +
          '<td class="actions"><button class="icon-btn" data-act="remove-row" data-row="' + i + '" title="刪除這一列">✕</button></td></tr>';
      }).join("") + "</tbody></table>" +
      '<div class="merge-actions"><button class="btn" data-act="add-row">＋ 新增一列</button><button class="btn primary" data-act="save">💾 儲存</button></div>';
  }
  function draftToData() {
    var values = {}, problems = [];
    varsDraft.rows.forEach(function (r) {
      var k = r.k.trim();
      if (!k && !r.v) return;
      if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(k)) problems.push("「" + (k || "（空白）") + "」不是合法的 KEY：只能用英文、數字、底線，不能數字開頭");
      else if (Object.prototype.hasOwnProperty.call(values, k)) problems.push("KEY「" + k + "」重複了");
      values[k] = r.v;
    });
    if (problems.length) throw new Error(problems.join("\n"));
    return { sensitive: varsDraft.sensitive, values: values };
  }
  async function loadVarsDraft(name) {
    var data = (await apiJson("GET", varsApi(name))).data || {};
    var values = data.values || {};
    varsDraft = { name: name, sensitive: !!data.sensitive, rows: Object.keys(values).map(function (k) { return { k: k, v: values[k], show: false }; }) };
    if (!varsDraft.rows.length) varsDraft.rows.push({ k: "", v: "", show: false });
    varsDirty = false;
  }
  function confirmDiscardVars() {
    return !varsDirty || confirm("「" + varsDraft.name + "」有修改還沒存，要放棄這些修改嗎？");
  }
  async function openVarsManager(selectName) {
    if (!currentProject) return;
    document.getElementById("varsTitle").textContent = "變數：" + currentProject.name;
    setVarsMsg("");
    varsDraft = null; varsDirty = false;
    try {
      setProjectVars((await apiJson("GET", projectApi())).vars || []);
      var name = selectName || (projectVars[0] && projectVars[0].name);
      if (name) await loadVarsDraft(name);
    } catch (err) { setVarsMsg("讀取變數檔失敗：" + err.message, true); }
    renderVarsFiles();
    renderVarsEditor();
    varsModal.hidden = false;
  }
  function closeVarsManager() {
    if (!confirmDiscardVars()) return;
    varsModal.hidden = true;
    varsDraft = null; varsDirty = false;
  }
  document.getElementById("varsManageBtn").addEventListener("click", function () { openVarsManager(); });
  document.getElementById("varsCloseBtn").addEventListener("click", closeVarsManager);
  varsModal.addEventListener("click", function (e) { if (e.target === varsModal) closeVarsManager(); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && !varsModal.hidden) closeVarsManager(); });
  varsFileList.addEventListener("click", async function (e) {
    var btn = e.target.closest("button[data-name]");
    if (!btn || (varsDraft && btn.dataset.name === varsDraft.name) || !confirmDiscardVars()) return;
    setVarsMsg("");
    try { await loadVarsDraft(btn.dataset.name); } catch (err) { setVarsMsg("讀取失敗：" + err.message, true); }
    renderVarsFiles();
    renderVarsEditor();
  });
  document.getElementById("varsNewForm").addEventListener("submit", async function (e) {
    e.preventDefault();
    var input = document.getElementById("varsNewName"), name = input.value.trim();
    if (!name || !confirmDiscardVars()) return;
    try {
      setProjectVars((await apiJson("PUT", varsApi(name), { sensitive: false, values: {} })).items);
      input.value = "";
      await loadVarsDraft(name);
      setVarsMsg("已新增「" + name + "」，填好變數按儲存");
    } catch (err) { setVarsMsg(err.status === 409 ? "已經有「" + name + "」了" : "新增失敗：" + err.message, true); }
    renderVarsFiles();
    renderVarsEditor();
  });
  varsEditor.addEventListener("input", function (e) {
    var input = e.target.closest("input[data-row]");
    if (!input) return;
    varsDraft.rows[Number(input.dataset.row)][input.dataset.field] = input.value;
    if (!varsDirty) {
      varsDirty = true;
      varsEditor.querySelector("h4").textContent = varsDraft.name + "（有修改還沒存）";
    }
  });
  varsEditor.addEventListener("change", function (e) {
    if (e.target.dataset.act === "sensitive") { varsDraft.sensitive = e.target.checked; varsDirty = true; renderVarsEditor(); }
    // KEY 改完，名稱變成像密碼（或反過來）才重畫，值換成遮起來的欄位；重畫後焦點放回使用者移過去的欄位
    else if (e.target.dataset.field === "k") {
      var row = Number(e.target.dataset.row), r = varsDraft.rows[row];
      var valueInput = varsEditor.querySelector('input[data-row="' + row + '"][data-field="v"]');
      if (valueInput.type === (SECRET_KEY_RE.test(r.k) && !r.show ? "password" : "text") &&
          !!varsEditor.querySelector('button[data-act="show"][data-row="' + row + '"]') === SECRET_KEY_RE.test(r.k)) return;
      var active = document.activeElement, focusRow = active && active.dataset ? active.dataset.row : null, focusField = active && active.dataset ? active.dataset.field : null;
      renderVarsEditor();
      if (focusRow != null && focusField) {
        var again = varsEditor.querySelector('input[data-row="' + focusRow + '"][data-field="' + focusField + '"]');
        if (again) again.focus();
      }
    }
  });
  varsEditor.addEventListener("click", async function (e) {
    var btn = e.target.closest("button[data-act]");
    if (!btn) return;
    var act = btn.dataset.act, row = Number(btn.dataset.row);
    if (act === "add-row") { varsDraft.rows.push({ k: "", v: "", show: false }); renderVarsEditor(); varsEditor.querySelector('tr:last-child input[data-field="k"]').focus(); return; }
    if (act === "remove-row") { varsDraft.rows.splice(row, 1); varsDirty = true; renderVarsEditor(); return; }
    if (act === "show") { varsDraft.rows[row].show = !varsDraft.rows[row].show; renderVarsEditor(); return; }
    var name = varsDraft.name;
    try {
      if (act === "save") {
        setProjectVars((await apiJson("PUT", varsApi(name) + "?overwrite=1", draftToData())).items);
        varsDirty = false;
        setVarsMsg("「" + name + "」已儲存");
      } else if (act === "rename" || act === "copy") {
        if (act === "copy" && varsDirty && !confirm("複製的是目前畫面上的內容（含還沒存的修改），確定嗎？")) return;
        var newName = (prompt(act === "rename" ? "新名稱（存好的批次裡用到的會一起改）：" : "複本名稱：", name + (act === "copy" ? "_copy" : "")) || "").trim();
        if (!newName || newName === name) return;
        if (act === "rename") {
          if (!confirmDiscardVars()) return;
          setProjectVars((await apiJson("POST", varsApi(name, "/rename"), { new_name: newName })).items);
          // 畫面上還沒存的批次設定、單份執行選的也跟著改名
          batchGroups.forEach(function (g) { g.cases.forEach(function (c) {
            if (c.login === name) c.login = newName;
            if (c.vars) c.vars = c.vars.map(function (v) { return v === name ? newName : v; });
          }); });
          runscriptVars = runscriptVars.map(function (v) { return v === name ? newName : v; });
          setProjectVars(projectVars);
        } else {
          setProjectVars((await apiJson("PUT", varsApi(newName), draftToData())).items);
        }
        await loadVarsDraft(newName);
        setVarsMsg(act === "rename" ? "已改名為「" + newName + "」" : "已複製成「" + newName + "」");
      } else if (act === "delete") {
        if (!confirm("確定刪除變數檔「" + name + "」？無法復原")) return;
        setProjectVars((await apiJson("DELETE", varsApi(name))).items);
        varsDraft = null; varsDirty = false;
        if (projectVars.length) await loadVarsDraft(projectVars[0].name);
        setVarsMsg("已刪除「" + name + "」");
      }
    } catch (err) { setVarsMsg((err.status === 409 ? "名稱已經有人用了：" : "失敗：") + err.message, true); }
    renderVarsFiles();
    renderVarsEditor();
  });

  // 測案用到的變數：列出哪些已經有變數檔、哪些還沒有，沒有的可以直接建立（新檔或加進現有的檔）
  var varsNeedModal = document.getElementById("varsNeedModal");
  var varsNeedBody = document.getElementById("varsNeedBody");
  var varsNeedCase = null;
  function missingVarKeys(testCase) { return uncoveredKeys(testCase, varFileNames()); }
  // auto＝LLM 剛產生的：全部都有變數檔就不打擾
  function openVarsNeed(testCase, auto) {
    if (!currentProject || !testCase) return;
    var keys = caseVarKeys(testCase);
    if (auto && !missingVarKeys(testCase).length) return;
    varsNeedCase = testCase;
    renderVarsNeed(keys.length ? "" : "這份測案沒有用到變數（${KEY}）。");
    varsNeedModal.hidden = false;
  }
  function renderVarsNeed(note) {
    var keys = caseVarKeys(varsNeedCase), missing = missingVarKeys(varsNeedCase);
    var filesOf = function (k) { return projectVars.filter(function (v) { return (v.keys || []).indexOf(k) >= 0; }).map(function (v) { return v.name; }); };
    var html = note ? '<div class="map-manager-hint">' + escapeHtml(note) + "</div>" : "";
    if (keys.length) {
      html += '<table class="map-table"><thead><tr><th>變數</th><th>在哪些變數檔</th></tr></thead><tbody>' + keys.map(function (k) {
        var files = filesOf(k);
        return '<tr><td class="vars-need-key">${' + escapeHtml(k) + "}</td><td>" + (files.length ? "✓ " + escapeHtml(files.join("、")) : '<span class="warn">還沒有</span>') + "</td></tr>";
      }).join("") + "</tbody></table>";
    }
    if (missing.length) {
      var secret = missing.some(function (k) { return SECRET_KEY_RE.test(k) || /LOGIN|ACCOUNT|ID$/i.test(k); });
      var defaultName = (varsNeedCase.test_name || "新變數檔").replace(/[\/\\:*?"<>|]/g, "_").slice(0, 60);
      html += '<div class="merge-subtitle">把還沒有的變數建立起來（值可以先空著，之後在「🧩 變數」補）</div>' +
        '<div class="field-row" style="gap:6px;flex-wrap:wrap;">' +
        '<label><input type="radio" name="varsNeedMode" value="new" checked> 新的變數檔</label>' +
        '<input type="text" id="varsNeedNewName" class="vars-input" style="width:auto;flex:1 1 180px;" value="' + escapeHtml(defaultName) + '">' +
        (projectVars.length ? '<label><input type="radio" name="varsNeedMode" value="existing"> 加進現有的</label>' +
          '<select id="varsNeedExisting" class="project-pick">' + projectVars.map(function (v) { return '<option value="' + escapeHtml(v.name) + '">' + escapeHtml(v.name) + "</option>"; }).join("") + "</select>" : "") +
        "</div>" +
        '<label class="field-row" style="gap:4px;"><input type="checkbox" id="varsNeedSensitive"' + (secret ? " checked" : "") + "> 敏感（新檔才會套用；帳密這類，匯出專案 zip 時不帶出去）</label>" +
        '<table class="map-table"><thead><tr><th></th><th>KEY</th><th>值</th></tr></thead><tbody>' + missing.map(function (k) {
          return '<tr><td><input type="checkbox" data-need-key="' + escapeHtml(k) + '" checked title="放進這個檔"></td><td class="vars-need-key">' + escapeHtml(k) + "</td>" +
            '<td><input class="vars-input" data-need-value="' + escapeHtml(k) + '" type="' + (SECRET_KEY_RE.test(k) ? "password" : "text") + '" autocomplete="off"></td></tr>';
        }).join("") + "</tbody></table>" +
        '<div class="map-manager-msg" id="varsNeedMsg" role="status"></div>' +
        '<div class="merge-actions"><button class="btn" data-act="later">稍後再說</button><button class="btn primary" data-act="create">建立</button></div>';
    } else if (keys.length) {
      html += '<div class="map-manager-hint">✓ 用到的變數都已經有變數檔了，執行時記得選對檔（批次：登入／＋ 變數；單份：變數檔）。</div>' +
        '<div class="merge-actions"><button class="btn primary" data-act="later">好</button></div>';
    } else {
      html += '<div class="merge-actions"><button class="btn primary" data-act="later">好</button></div>';
    }
    varsNeedBody.innerHTML = html;
  }
  function closeVarsNeed() { varsNeedModal.hidden = true; varsNeedCase = null; }
  document.getElementById("varsNeedCloseBtn").addEventListener("click", closeVarsNeed);
  varsNeedModal.addEventListener("click", function (e) { if (e.target === varsNeedModal) closeVarsNeed(); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && !varsNeedModal.hidden) closeVarsNeed(); });
  llmgenVarsCheckBtn.addEventListener("click", function () { openVarsNeed(llmgenLatestCase, false); });
  varsNeedBody.addEventListener("click", async function (e) {
    var btn = e.target.closest("button[data-act]");
    if (!btn) return;
    if (btn.dataset.act === "later") { closeVarsNeed(); return; }
    var msg = document.getElementById("varsNeedMsg");
    var picked = {};
    varsNeedBody.querySelectorAll("input[data-need-key]").forEach(function (cb) {
      if (cb.checked) picked[cb.dataset.needKey] = varsNeedBody.querySelector('input[data-need-value="' + cb.dataset.needKey + '"]').value;
    });
    if (!Object.keys(picked).length) { msg.textContent = "至少勾一個變數"; msg.className = "map-manager-msg error"; return; }
    var modeEl = varsNeedBody.querySelector('input[name="varsNeedMode"]:checked');
    var toExisting = modeEl && modeEl.value === "existing";
    var name = toExisting ? document.getElementById("varsNeedExisting").value : document.getElementById("varsNeedNewName").value.trim();
    if (!name) { msg.textContent = "請輸入變數檔名稱"; msg.className = "map-manager-msg error"; return; }
    btn.disabled = true;
    try {
      var res;
      if (toExisting) {
        var data = (await apiJson("GET", varsApi(name))).data || {};
        data.values = Object.assign({}, data.values || {}, picked);
        res = await apiJson("PUT", varsApi(name) + "?overwrite=1", data);
      } else {
        res = await apiJson("PUT", varsApi(name), { sensitive: document.getElementById("varsNeedSensitive").checked, values: picked });
      }
      setProjectVars(res.items);
      var left = missingVarKeys(varsNeedCase);
      renderVarsNeed((toExisting ? "已加進「" : "已建立「") + name + "」。" + (left.length ? "還有沒建立的變數，可以再建一個檔（例如帳密跟測試資料分開）。" : ""));
      setProjectStatus("變數檔「" + name + "」已存進專案");
    } catch (err) {
      msg = document.getElementById("varsNeedMsg");
      msg.textContent = (err.status === 409 ? "已經有「" + name + "」了，換個名稱或選「加進現有的」" : "建立失敗：" + err.message);
      msg.className = "map-manager-msg error";
      btn.disabled = false;
    }
  });

  function batchItemToSaved(item) {
    if (!item) return null;
    return item.ref ? { ref: item.ref } : { name: item.name, case: item.case };
  }
  batchSaveToProjectBtn.addEventListener("click", async function () {
    if (!currentProject || !batchGroups.length) return;
    var name = (prompt("存成專案批次，名稱（也是檔名）：", "batch_" + new Date().toISOString().slice(0, 10)) || "").trim();
    if (!name) return;
    var data = { batch_version: 2, saved_at: new Date().toISOString(), options: batchOptions(),
      groups: batchGroups.map(function (g) {
        var out = g.mapRef ? { map_ref: g.mapRef } : { map_name: g.mapName, map: g.map };
        out.setup_case = batchItemToSaved(g.setup);
        if (g.setupMapRef) out.setup_map_ref = g.setupMapRef;
        out.cases = g.cases.map(function (c) {
          var saved = batchItemToSaved(c);
          if (c.login) saved.login = c.login;
          if (c.vars && c.vars.length) saved.vars = c.vars.slice();
          return saved;
        });
        return out;
      }) };
    try {
      if (await saveProjectItem("batches", name, data)) setProjectStatus("批次「" + name + "」已存進專案");
    } catch (err) { alert("存檔失敗：" + err.message); }
  });
  batchProjectSelect.addEventListener("change", async function () {
    var name = batchProjectSelect.value;
    batchProjectSelect.value = "";
    if (!name || batchRunning) return;
    if (batchGroups.length && !confirm("開啟會取代目前的批次設定，確定嗎？")) return;
    try {
      var data = (await apiJson("GET", projectApi("/batches/" + encodeURIComponent(name)))).data;
      await loadBatchConfig(data);
    } catch (err) { alert("開啟批次失敗：" + err.message); }
  });

  async function initProjects() {
    renderProjectPickers();
    if (!/^https?:$/.test(location.protocol)) return;
    try { await loadProjectList(); }
    catch (err) { setProjectStatus("⚠️ 讀不到專案清單：" + err.message, true); return; }
    var last = null;
    try { last = localStorage.getItem("lastProject"); } catch (e) { /* 照預設 */ }
    // aitest start <專案> 打開的網址會帶 ?project=，優先開這個
    var fromUrl = new URLSearchParams(location.search).get("project");
    if (fromUrl) {
      last = fromUrl;
      // 只在打開頁面時用一次就拿掉：之後在網頁上切了別的專案，重新整理不會又被拉回網址指定的那個
      try { history.replaceState(null, "", location.pathname); } catch (e) { /* 改不了網址不影響使用 */ }
    }
    var ids = projectList.map(function (p) { return p.id; });
    var pick = ids.indexOf(last) >= 0 ? last : (ids.length === 1 && last !== "" ? ids[0] : null);
    if (pick) { await openProject(pick); return; }
    setProjectStatus(ids.length ? "選一個專案開始，地圖、測案都會自動存在專案裡"
      : "還沒有專案：用 uv run aitest-workspace create 建立（見 README 3.3），或匯入其他人給的專案 zip");
  }

  initScenarioFromCurrentMap();
  initLlmgenFromCurrentMap();
  renderRunscriptPreview();

  // boot: select root
  var root = screens.filter(function (s) { return !s.parent; })[0] || screens[0];
  if (root) selectScreen(root.uid); else renderEmptyMapDetail();
  initProjects();
})();
